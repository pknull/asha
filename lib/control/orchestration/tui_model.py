"""Pure initiative presentation state for the Control TUI. This module performs no I/O."""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Iterable
from ..tui_style import display_state, rail_tiers
from .model import (
    ATTEMPT_NONTERMINAL_STATES,
    ATTEMPT_TERMINAL_STATES,
    COORDINATOR_LIVE_STATES,
    INITIATIVE_TERMINAL_STATES,
    unanswered_operator_question,
)


PARKED_COORDINATOR_ATTENTION_SECONDS = 300

# The two session-local retained views. `current` is the default: a loaded head
# stays only for proved current activity, genuine unresolved demand, or source
# evidence too incomplete to call it quiet. `all` adds every other retained
# head the loader read, including bounded archived head metadata.
RETAINED_VIEW_SCOPES = ("current", "all")
_QUIET_PLAN_STATES = frozenset({"draft", "planning"})
# A Control task row in one of these states is no longer a live worker. Any
# other state (including `idle` and `unknown`) is a session that still exists
# or an observation that failed, never proof that the work ended.
_TASK_ENDED_STATES = frozenset({"exited", "failed", "ended", "archived"})


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
    live_work: bool = False

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.kind, self.initiative_id, self.id)

    @property
    def needs_human(self) -> bool:
        return self.kind != "room" and self.attention not in ("-", "")


def _coordinator_text(view: dict[str, Any]) -> str:
    record = view.get("coordinator")
    if not record:
        return "-"
    state = record.get("state")
    if state in {"active", "waiting", "needs-input", "stopping", "starting"}:
        return f"{record.get('harness', '?')} g{record.get('generation', '?')}"
    return f"{state} g{record.get('generation', '?')}"


def _nodes_text(view: dict[str, Any]) -> str:
    """``done/total``, or ``?`` when this head's node records were never read.

    An archived head's graph is deliberately not loaded and a damaged head's
    could not be. Neither is the same as a graph with no nodes, so neither may
    render ``0/0``: that would assert a count over records nobody opened.
    """
    if view.get("_metadata_only") or view.get("_graph_unavailable"):
        return "?"
    nodes = view.get("nodes", [])
    done = sum(1 for node in nodes if node.get("state") == "succeeded")
    return f"{done}/{len(nodes)}"


_TASKS_ROOT_KEY = ("tasks-root", "unbound", "unbound")
_ROOMS_ROOT_KEY = ("rooms-root", "rooms", "rooms")
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
        return "at prompt" + (f": {detail}" if detail else "")
    blocker = reconciliation.get("blocker")
    if blocker:
        return str(blocker)
    return "-"


def _parked(view: dict[str, Any]) -> bool:
    """A paused initiative parks its durable node demand until it resumes."""
    return view["initiative"].get("state") == "paused"


def _node_attention(
    initiative_state: str | None,
    node_state: str | None,
    coordinator_parked: bool,
    worker_attention: str,
    directive: str = "-",
) -> str:
    """WAITING ON text for one node row, identical on full and patched refreshes.

    Durable node demand (a pending decision, a parked coordinator) is parked
    with its initiative. A live worker observation (a prompt on screen, an exit
    the operator must close, a process blocker) is never parked: that ask is
    happening now regardless of the initiative's schedule. A directive whose
    target is still live, or whose binding cannot be resolved, is such an
    observation and stays readable beneath a parked head; one whose target is
    proved sealed and ended is retained history and passes `-` in here.
    """
    if initiative_state == "paused":
        return worker_attention if worker_attention not in ("-", "") else directive
    if coordinator_parked:
        return "coordinator parked"
    if node_state == "needs-input":
        return "needs input"
    if worker_attention not in ("-", ""):
        return worker_attention
    return directive if directive not in ("", None) else worker_attention


def _link_maps(views: list[dict[str, Any]]) -> tuple[dict[str, tuple[str, str]], dict[str, str]]:
    """(control_task_id -> (initiative_id, attempt_id), attempt_id -> control_task_id).

    Built from every loaded head, before any presentation filter, so a task
    owned by a head the current view hides is still a bound task. Both durable
    ownership records are read: the link a dispatched attempt writes, and the
    attempt's own `task_id` reservation, which exists first. Reading only links
    listed a reserved-but-unlinked worker as unbound.
    """
    bound: dict[str, tuple[str, str]] = {}
    by_attempt: dict[str, str] = {}
    for view in views:
        initiative_id = view["initiative"]["initiative_id"]
        for attempt in view.get("attempts", []) or []:
            task_id = attempt.get("task_id")
            attempt_id = attempt.get("attempt_id")
            if isinstance(task_id, str) and isinstance(attempt_id, str):
                bound.setdefault(task_id, (initiative_id, attempt_id))
                by_attempt.setdefault(attempt_id, task_id)
        for link in view.get("links", []) or []:
            # The link is the authoritative binding; it overwrites a reservation.
            bound[link["control_task_id"]] = (initiative_id, link["attempt_id"])
            by_attempt[link["attempt_id"]] = link["control_task_id"]
    return bound, by_attempt


def _view_complete(view: dict[str, Any]) -> bool:
    """Whether the loader read this head's records without truncation or loss.

    Both the observation as a whole and this head in particular count. A
    bounded pass that read every other head perfectly still cannot call this
    one complete when its own graph was short, unreadable, or bound to a plan
    record the pass never returned.

    Absent metadata means a hand-built or historical view, which is read as
    complete. The loader attaches its own bounded-read summary.
    """
    if view.get("_graph_unavailable"):
        return False
    if view.get("_graph_complete") is False:
        return False
    completeness = view.get("_completeness")
    if not isinstance(completeness, dict):
        return True
    return bool(completeness.get("complete", True))


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


def _utc_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.endswith("Z"):
        return None
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return None
    return parsed if parsed.tzinfo == timezone.utc else None


def parked_ready_nodes(
    view: dict[str, Any], *, now: datetime | None = None,
) -> tuple[str, ...]:
    """Ready zero-attempt nodes whose live coordinator is no longer watching.

    ``waiting`` is the durable armed-watch state. ``active`` means the wait
    command returned; only after the named grace period does that parked state
    become human-actionable. Recent coordinator-authored journal activity also
    refreshes the grace period without trusting worker prose.
    """
    precomputed = view.get("_parked_ready_nodes")
    if now is None and isinstance(precomputed, (list, tuple)):
        return tuple(str(node_id) for node_id in precomputed)
    coordinator = view.get("coordinator")
    if not isinstance(coordinator, dict) or view.get("coordinator_live") is False:
        return ()
    state = coordinator.get("state")
    if state not in {"active", "waiting"}:
        return ()
    activity = _utc_timestamp(coordinator.get("updated_at"))
    for event in view.get("events", []) or []:
        if event.get("actor_kind") != "coordinator":
            continue
        observed = _utc_timestamp(event.get("recorded_at"))
        if observed is not None and (activity is None or observed > activity):
            activity = observed
    if activity is None:
        return ()
    observed_now = now or datetime.now(timezone.utc)
    if observed_now.tzinfo != timezone.utc:
        raise ValueError("parked coordinator observation time must be UTC")
    age = (observed_now - activity).total_seconds()
    if state == "waiting":
        # A wait cannot remain armed past the coordinator command's hard
        # ceiling. This bounded lease keeps a killed wait process from leaving
        # a generation falsely healthy forever.
        from .coordinator import MAX_COORDINATOR_WAIT_SECONDS
        if age < MAX_COORDINATOR_WAIT_SECONDS + PARKED_COORDINATOR_ATTENTION_SECONDS:
            return ()
    elif age < PARKED_COORDINATOR_ATTENTION_SECONDS:
        return ()
    attempted = {
        item.get("node_id") for item in view.get("attempts", []) or []
    }
    return tuple(sorted(
        node["node_id"] for node in view.get("nodes", []) or []
        if node.get("state") == "ready" and node.get("node_id") not in attempted
    ))


def _directive_binding(
    view: dict[str, Any],
    action: dict[str, Any],
    outcome: dict[str, Any],
    by_attempt: dict[str, str],
    task_index: dict[str, Any],
) -> tuple[str, str]:
    """Classify one retained pending directive against its own bound records.

    Returns ``(classification, reason)`` where classification is:

    ``live``      the exact node/attempt/active-plan binding resolves to work
                  that has not ended, so the ask is still the operator's.
    ``deferred``  the binding resolves and proves the target already sealed or
                  ended. The record is retained history, not current demand;
                  nothing is delivered, acknowledged, or deleted for it.
    ``unknown``   the binding is missing, foreign, malformed, stale, or
                  contradicted by the observations at hand. Uncertainty is
                  shown, never silently discharged and never read as authority.

    Delivery is `pending` for every one of these; `pending` alone never proved
    a live ask, which is exactly the defect this classification replaces.
    """
    node_id = outcome.get("node_id")
    attempt_id = outcome.get("attempt_id")
    if not isinstance(node_id, str) or not node_id:
        return "unknown", "no node binding is recorded"
    if not isinstance(attempt_id, str) or not attempt_id:
        return "unknown", "no attempt binding is recorded"
    plan_binding = view["initiative"].get("active_plan")
    active_plan = (
        plan_binding.get("digest") if isinstance(plan_binding, dict) else None
    )
    action_plan = action.get("active_plan_digest")
    if isinstance(active_plan, str) and isinstance(action_plan, str) and action_plan != active_plan:
        return "unknown", "it is bound to a plan that is no longer active"
    attempt = next(
        (item for item in view.get("attempts", []) or []
         if item.get("attempt_id") == attempt_id),
        None,
    )
    if attempt is None:
        return "unknown", "its attempt record is not among the loaded records"
    if attempt.get("node_id") != node_id:
        return "unknown", "its node and attempt bindings disagree"
    if not any(node.get("node_id") == node_id for node in view.get("nodes", []) or []):
        return "unknown", "its node record is not among the loaded records"
    task_id = by_attempt.get(attempt_id) or attempt.get("task_id")
    row = None if task_id is None else task_index.get(task_id)
    observed = None if row is None else getattr(row, "display_state", None)
    ended = observed in _TASK_ENDED_STATES
    state = attempt.get("state")
    if state not in ATTEMPT_TERMINAL_STATES and state not in ATTEMPT_NONTERMINAL_STATES:
        return "unknown", "its attempt records a state this controller cannot read"
    if state in ATTEMPT_TERMINAL_STATES:
        if row is not None and not ended:
            return "unknown", "its attempt is sealed while its worker still reads live"
        return "deferred", "its attempt is sealed and carries no live worker"
    if ended:
        return "unknown", "its attempt is unsealed while its worker has ended"
    return "live", ""


def _pending_directives(
    view: dict[str, Any],
    by_attempt: dict[str, str] | None = None,
    task_index: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Every retained directive whose delivery is still pending, classified.

    The retained record is never dropped here: a deferred directive keeps its
    row in this history so the retained view and the detail pane can show it.
    Only `attention_items` narrows the list to what is demand right now.
    """
    by_attempt = {} if by_attempt is None else by_attempt
    task_index = {} if task_index is None else task_index
    pending = []
    for action in view.get("actions", []) or []:
        if action.get("action_class") != "directive":
            continue
        try:
            outcome = json.loads(action.get("outcome") or "{}")
        except (TypeError, ValueError):
            pending.append({
                "action_id": action.get("action_id"), "node_id": None,
                "attempt_id": None, "classification": "unknown",
                "reason": "its recorded outcome cannot be read",
            })
            continue
        if not isinstance(outcome, dict):
            # Well-formed JSON of the wrong shape -- `[]`, `"pending"`, `7` --
            # is exactly as unreadable as a truncated record: the delivery
            # field it would have carried is not there to read. It shares the
            # parse failure's branch rather than the ordinary
            # not-pending branch, because dropping it would silently
            # discharge a directive whose binding was never resolved.
            pending.append({
                "action_id": action.get("action_id"), "node_id": None,
                "attempt_id": None, "classification": "unknown",
                "reason": "its recorded outcome is not a directive record",
            })
            continue
        if outcome.get("delivery") != "pending":
            continue
        classification, reason = _directive_binding(
            view, action, outcome, by_attempt, task_index,
        )
        pending.append({
            "action_id": action.get("action_id"), "node_id": outcome.get("node_id"),
            "attempt_id": outcome.get("attempt_id"),
            "classification": classification, "reason": reason,
        })
    return pending


def _directive_items(
    view: dict[str, Any], by_attempt: dict[str, str], task_index: dict[str, Any],
) -> list[dict[str, Any]]:
    """The pending directives that are demand now, live or honestly unknown."""
    items = []
    loaded_nodes = {
        node.get("node_id") for node in view.get("nodes", []) or []
    }
    for directive in _pending_directives(view, by_attempt, task_index):
        if directive["classification"] == "deferred":
            continue
        reason = directive["reason"]
        node_id = directive.get("node_id")
        items.append({
            # A node binding no loaded node record answers cannot address a
            # node row, so the ask belongs to the head instead of vanishing.
            "kind": "directive-pending",
            "node_id": node_id if node_id in loaded_nodes else None,
            "detail": (
                f"directive {directive['action_id'] or 'with no recorded id'} "
                "awaits delivery" + (f"; {reason}" if reason else "")
            ),
            "resolution": (
                "relay to the attempt's pane or let the next attempt carry it"
                if directive["classification"] == "live" else
                "verify the directive's target before relaying; nothing here delivers it"
            ),
            "certainty": directive["classification"],
        })
    return items


def initiative_demand(
    view: dict[str, Any],
    *,
    by_attempt: dict[str, str] | None = None,
    task_index: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Everything on one loaded initiative that waits on a human, classified once.

    This is the single demand projection. `attention_items` (and so
    ``asha initiative attention``) is this list across every loaded head, and
    the tree's WAITING ON text, its `!` filter, and the Current/All retained
    classification all read the same entries. There is no second assembler.

    Every entry carries `certainty`: `live` when the records prove the ask,
    `unknown` when the evidence is missing, stale, foreign, or self-
    contradictory. An unknown entry stays listed; absent evidence is never
    read as an absent ask.
    """
    by_attempt = {} if by_attempt is None else by_attempt
    task_index = {} if task_index is None else task_index
    initiative = view["initiative"]
    initiative_id = initiative["initiative_id"]
    state = initiative.get("state")
    items: list[dict[str, Any]] = []
    if view.get("_metadata_only") or view.get("_graph_unavailable"):
        # Archived head metadata is read-only history and a damaged head is
        # missing evidence: in neither case was a graph loaded, so no demand
        # may be asserted -- or denied -- from it. `retained_classification`
        # keeps the damaged head visible and uncertain rather than quiet.
        return items
    # A capped or damaged event sample is not the journal. The classifiers
    # below that reason causally over event history are told so, so that a
    # sample can never discharge a question or prove a coordinator idle.
    events_complete = view.get("_events_complete", True) is not False
    # A paused initiative parks its durable node demand (pending decisions, a
    # parked coordinator); the live observations below stay listed.
    parked_initiative = _parked(view)
    parked = set() if parked_initiative else set(parked_ready_nodes(view))
    if state == "awaiting-plan-approval":
        plan = view.get("plan") or {}
        items.append({
            "kind": "plan-approval",
            "detail": f"plan revision {plan.get('revision', '?')} awaits approval",
            "resolution": f"asha initiative approve {initiative_id} --digest {plan.get('digest', '?')}",
            "certainty": "live",
        })
    if state == "needs-input":
        # The head itself waits on the operator, with or without any node
        # or approval demand beneath it: the tree shows `needs you`, so the
        # verb lists it too. The question is quoted when its event is in
        # the loaded tail; the durable head state is the demand either way.
        #
        # The causal answer-discharge classifier only ever sees a history the
        # observation proved contiguous and whole. Given a capped or damaged
        # sample it is not consulted at all: a sample cannot show the edge
        # that would discharge the wait, and reading its silence as an answer
        # would retire a question the operator still owns. The demand stays,
        # marked unknown, which is the safe direction.
        question = (
            unanswered_operator_question(
                view.get("events", []) or [],
                actions=view.get("actions", []) or [],
                initiative=initiative,
            )
            if events_complete else None
        )
        asked = None if question is None else question.get("payload", {}).get("question")
        items.append({
            "kind": "operator-decision",
            "detail": (
                f"operator decision: {asked}" if asked
                else "initiative waits on the operator"
            ),
            "resolution": f"answer, then asha initiative resume {initiative_id}",
            "certainty": "live" if events_complete else "unknown",
        })
    if state == "ready-for-integration":
        # The tree has always shown `integrate` here while the verb omitted
        # it. One projection cannot say both; the demand is real and named.
        items.append({
            "kind": "integration",
            "detail": "candidate work is ready for the operator's integration decision",
            "resolution": (
                "review the candidate and land it yourself; this tree performs no "
                "automated integration"
            ),
            "certainty": "live",
        })
    if state == "approved":
        items.append({
            "kind": "activation",
            "detail": "an approved plan awaits activation",
            "resolution": f"asha initiative activate {initiative_id}",
            "certainty": "live",
        })
    for approval in view.get("approvals", []) or []:
        from .current_actions import approval_demand
        demand = approval_demand(approval, initiative)
        if demand is not None and demand["disposition"] not in {"expired", "stale-plan", "inactive", "deferred"}:
            items.append(demand)
    directives = _directive_items(view, by_attempt, task_index)
    for node in view.get("nodes", []) or []:
        if node["node_id"] in parked:
            items.append({
                "kind": "coordinator-parked", "node_id": node["node_id"],
                "detail": (
                    f"node {node['node_id']} is ready with zero attempts and "
                    f"no coordinator activity for at least "
                    f"{PARKED_COORDINATOR_ATTENTION_SECONDS} seconds"
                ),
                "resolution": (
                    f"asha initiative coordinator attach "
                    f"{initiative_id}; resume the event watch "
                    "or launch a replacement coordinator"
                ),
                # The grace period is refreshed by coordinator-authored journal
                # activity, so a capped sample can only understate it. The ask
                # is shown either way; it is marked unknown when the sample
                # could not prove the coordinator was idle.
                "certainty": "live" if events_complete else "unknown",
            })
        if node.get("state") == "needs-input" and not parked_initiative:
            items.append({
                "kind": "needs-input", "node_id": node["node_id"],
                "detail": f"node {node['node_id']} needs a decision",
                "resolution": "decide or repair, then resume",
                "certainty": "live",
            })
        attempt = _latest_attempt(view, node["node_id"])
        worker, attention, task_id, _seen = _attempt_worker(attempt, by_attempt, task_index)
        del worker
        if attention not in ("-", ""):
            items.append({
                "kind": "worker", "node_id": node["node_id"],
                "task_id": task_id, "detail": attention,
                "resolution": "attach (Enter) or close (X) in asha control",
                "certainty": "live",
            })
    items.extend(directives)
    failed = sorted(
        node["node_id"] for node in view.get("nodes", []) or []
        if node.get("state") == "failed"
    )
    # A terminal head's failures are its history: the STATE column and the rail
    # already say so, and nothing is waiting on the operator to act on them.
    if failed and not parked_initiative and state not in INITIATIVE_TERMINAL_STATES:
        items.append({
            "kind": "failed-nodes", "failed": len(failed),
            "detail": f"{len(failed)} failed node(s): " + ", ".join(failed[:6]),
            "resolution": "repair, retry, or stop the initiative; nothing retries on its own here",
            "certainty": "live",
        })
    return items


def _head_attention(view: dict[str, Any], demand: list[dict[str, Any]]) -> str:
    """The head row's WAITING ON text, read from the one demand projection.

    Priority is what the operator loses last. A paused head short-circuits to
    `-` before the durable node demand below it, because parking is status:
    the STATE column and the held rail already say paused. Live asks beneath a
    parked head keep their own node rows, so `!` still finds the head by them.
    """
    kinds = {item["kind"] for item in demand}
    for kind, label in (
        ("plan-approval", "plan approval"),
        ("operator-decision", "needs input"),
        ("integration", "integrate"),
        ("activation", "activate"),
        ("salvage-approval", "salvage approval"),
        ("review-budget-approval", "review retry"),
        ("approval-inspection", "inspect approval"),
    ):
        if kind in kinds:
            return label
    # A directive no loaded node row can carry has nowhere else to be shown,
    # so it reaches the head even while the head is parked: an unresolved or
    # still-live directive is an ask happening now, not the head's schedule.
    unbound_directive = any(
        item["kind"] == "directive-pending" and item.get("node_id") is None
        for item in demand
    )
    if view["initiative"].get("state") == "paused":
        return "directive pending" if unbound_directive else "-"
    if "coordinator-parked" in kinds:
        return "coordinator parked"
    failed = next(
        (item["failed"] for item in demand if item["kind"] == "failed-nodes"), 0,
    )
    if failed:
        return f"{failed} failed"
    if unbound_directive:
        return "directive pending"
    return "-"


def retained_classification(
    view: dict[str, Any],
    *,
    demand: list[dict[str, Any]] | None = None,
    by_attempt: dict[str, str] | None = None,
    task_index: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Whether one loaded head belongs in the Current view, and on what evidence.

    Returns ``{"current": bool, "reason": str, "certain": bool}``. `current`
    is never taken from a status word alone: a `running` head with nothing
    observed running is not current, and a `paused` head with a live
    coordinator or an unsealed attempt is. `certain` is False wherever the
    verdict rests on evidence that is missing rather than negative, so the
    counts can say so.
    """
    by_attempt = {} if by_attempt is None else by_attempt
    task_index = {} if task_index is None else task_index
    if demand is None:
        demand = initiative_demand(view, by_attempt=by_attempt, task_index=task_index)
    initiative = view["initiative"]
    state = initiative.get("state")
    if view.get("_graph_unavailable"):
        # The head is retained and its own record was read; its graph was not.
        # That is missing evidence, never proof of quiet, so the head stays in
        # Current and says the verdict rests on records it could not read.
        return {
            "current": True,
            "reason": "its retained records could not be read",
            "certain": False,
        }
    if view.get("_metadata_only") or state == "archived":
        return {"current": False, "reason": "archived head metadata", "certain": True}
    if demand:
        unknown = any(item.get("certainty") == "unknown" for item in demand)
        return {
            "current": True,
            "reason": "unresolved demand" if not unknown else "demand that cannot be resolved",
            "certain": not unknown,
        }
    if not _view_complete(view):
        return {"current": True, "reason": "incomplete source evidence", "certain": False}
    coordinator = view.get("coordinator")
    if isinstance(coordinator, dict) and coordinator.get("state") in COORDINATOR_LIVE_STATES:
        live = view.get("coordinator_live")
        if live is not False:
            return {
                "current": True, "reason": "coordinator activity", "certain": live is True,
            }
    attempts = view.get("attempts", []) or []
    for attempt in attempts:
        if attempt.get("state") not in ATTEMPT_NONTERMINAL_STATES:
            continue
        task_id = by_attempt.get(attempt.get("attempt_id")) or attempt.get("task_id")
        row = None if task_id is None else task_index.get(task_id)
        observed = None if row is None else getattr(row, "display_state", None)
        return {
            "current": True, "reason": "task activity",
            "certain": observed is not None and observed not in _TASK_ENDED_STATES,
        }
    nodes = view.get("nodes", []) or []
    if state == "paused":
        return {"current": False, "reason": "quiet paused", "certain": True}
    if state not in INITIATIVE_TERMINAL_STATES:
        if state not in _QUIET_PLAN_STATES and not nodes:
            return {
                "current": True, "reason": "a started head with no node records",
                "certain": False,
            }
        attempted = {item.get("node_id") for item in attempts}
        if any(
            node.get("state") in {"ready", "dispatching"}
            and node.get("node_id") not in attempted
            for node in nodes
        ):
            # Work the plan says is ready that nothing has picked up. The
            # parked-coordinator rule only fires while a coordinator is live,
            # so without this the stall would silently leave the tree.
            return {
                "current": True, "reason": "ready work with no attempt", "certain": False,
            }
    if state in _QUIET_PLAN_STATES:
        return {"current": False, "reason": "queued unfinished", "certain": True}
    if state in INITIATIVE_TERMINAL_STATES:
        return {"current": False, "reason": "settled", "certain": True}
    return {"current": False, "reason": "quiet", "certain": True}


def attention_items(
    views: list[dict[str, Any]], task_rows: Iterable[Any] = (),
) -> list[dict[str, Any]]:
    """Everything currently waiting on a human, across initiatives and tasks.

    One assembler feeds both the tree's waiting-on-me filter and the
    ``asha initiative attention`` verb, so the two can never disagree. This is
    the complete bounded demand projection over the loaded heads: no
    session-local view or filter narrows it, and its item count is the number
    of asks, which is larger than the number of tree rows whenever one head
    carries several.
    """
    items: list[dict[str, Any]] = []
    bound, by_attempt = _link_maps(views)
    task_index = {getattr(row, "task", {}).get("task_id"): row for row in task_rows}
    for view in views:
        identity = _identity(view)
        for item in initiative_demand(
            view, by_attempt=by_attempt, task_index=task_index,
        ):
            items.append({**identity, **item})
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
                "certainty": "live",
            })
    return items


def _identity(view: dict[str, Any]) -> dict[str, Any]:
    initiative = view["initiative"]
    return {
        "initiative_id": initiative["initiative_id"],
        "slug": initiative.get("slug", ""),
    }


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
        room_rows: Iterable[dict[str, Any]] = (),
        rooms_error: str | None = None,
        view_scope: str = "current",
    ) -> None:
        if view_scope not in RETAINED_VIEW_SCOPES:
            raise ValueError("view_scope must be current or all")
        self.views = [copy.deepcopy(view) for view in views]
        self.height = max(0, int(height))
        self.width = max(0, int(width))
        # The unbound-tasks branch starts open: plain Control tasks must stay
        # visible without a keystroke, exactly as the old Tasks view showed them.
        self.expanded: set[tuple[str, str]] = (
            {("tasks-root", "unbound"), ("rooms-root", "rooms")}
            if expanded is None else set(expanded)
        )
        self.selection = selection
        self.filter_string = filter_string
        self.task_rows: tuple[Any, ...] = tuple(task_rows)
        self.attention_only = bool(attention_only)
        # Session-local presentation only. It narrows what this terminal draws
        # and never what is loaded, reconciled, recorded, or reported by the
        # CLI's own demand projection.
        self.view_scope = view_scope
        self.retained_counts: dict[str, Any] = {
            "shown": 0, "loaded": 0, "hidden": 0, "partial": False, "unknown": False,
        }
        self.binding_complete = True
        self.orchestration_error = orchestration_error
        self.room_rows: list[dict[str, Any]] = copy.deepcopy(list(room_rows))
        self.rooms_error = rooms_error
        self.scroll_offset = 0
        self.help_visible = False
        self.message: str | None = None
        self.pane: str = "summary"
        self._data_revision = 0
        self._rows_cache: tuple[InitiativeRow, ...] | None = None
        self._rows_cache_key: tuple[Any, ...] | None = None
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

    def _current_rows_key(self) -> tuple[Any, ...]:
        # The view scope and the loaded records' completeness both change which
        # heads a rebuild produces, so a cached row set from another scope or
        # another completeness must never be reused. Every completeness the
        # classification reads is part of the identity: whether the head's own
        # graph was readable, whether it was archived metadata, and whether its
        # event history was the journal or only a sample.
        return (
            self._data_revision, frozenset(self.expanded),
            self.filter_string, self.attention_only, self.view_scope,
            tuple(sorted(
                (view["initiative"]["initiative_id"], _view_complete(view),
                 bool(view.get("_metadata_only")),
                 bool(view.get("_graph_unavailable")),
                 view.get("_events_complete", True) is not False)
                for view in self.views
            )),
        )

    def rows(self) -> list[InitiativeRow]:
        key = self._current_rows_key()
        if self._rows_cache is None or self._rows_cache_key != key:
            self._rows_cache = tuple(self._build_rows())
            self._rows_cache_key = key
        return list(self._rows_cache)

    def _classify(
        self, view: dict[str, Any], by_attempt: dict[str, str],
        task_index: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        demand = initiative_demand(view, by_attempt=by_attempt, task_index=task_index)
        verdict = retained_classification(
            view, demand=demand, by_attempt=by_attempt, task_index=task_index,
        )
        return demand, verdict

    def _recount(
        self, rows: list[InitiativeRow], by_attempt: dict[str, str],
        task_index: dict[str, Any],
    ) -> None:
        """Refresh the retained counts after a patch that skipped `_build_rows`.

        A task-only patch reuses the cached rows, so nothing else recomputes
        the header's shown/loaded/hidden counts or their honesty markers. The
        heads on screen are unchanged by construction (the patch is refused
        when a classification crosses the view boundary), but a worker fact can
        still move a shown head between proved and unknown.
        """
        drawn = {row.initiative_id for row in rows if row.kind == "initiative"}
        shown = len(drawn)
        self.retained_counts = {
            "shown": shown, "loaded": len(self.views),
            "hidden": max(0, len(self.views) - shown),
            "partial": any(not _view_complete(view) for view in self.views),
            "unknown": any(
                not retained_classification(
                    view, by_attempt=by_attempt, task_index=task_index,
                )["certain"]
                for view in self.views
                if view["initiative"]["initiative_id"] in drawn
            ),
        }

    def _in_scope(self, verdict: dict[str, Any]) -> bool:
        return self.view_scope == "all" or bool(verdict["current"])

    def _build_rows(self) -> list[InitiativeRow]:
        needle = self.filter_string.casefold()
        # Task ownership is resolved over every loaded head, including the ones
        # this view hides, before any presentation filter runs. Filtering first
        # would have listed a hidden head's worker as an unbound task.
        bound, by_attempt = _link_maps(self.views)
        task_index = {
            getattr(row, "task", {}).get("task_id"): row for row in self.task_rows
        }
        rows: list[InitiativeRow] = self._room_branch(needle)
        loaded = 0
        shown = 0
        partial = False
        unknown = False
        for view in self._sorted_views():
            loaded += 1
            initiative = view["initiative"]
            initiative_id = initiative["initiative_id"]
            if not _view_complete(view):
                partial = True
            demand, verdict = self._classify(view, by_attempt, task_index)
            if not self._in_scope(verdict):
                continue
            if not verdict["certain"]:
                unknown = True
            directives = {
                item["node_id"]: "directive pending"
                for item in demand
                if item["kind"] == "directive-pending" and item.get("node_id")
            }
            head = InitiativeRow(
                "initiative", 0, initiative_id, initiative_id,
                initiative.get("slug", initiative_id[:8]), initiative.get("state", "?"),
                "initiative", _coordinator_text(view), _nodes_text(view),
                _head_attention(view, demand),
                rail=tuple(rail_tiers(view)), display=display_state(view),
                live_work=any(
                    item.get("state") in {"running", "dispatching"}
                    for records in (view.get("nodes", []), view.get("attempts", []))
                    for item in records
                ),
            )
            children: list[InitiativeRow] = []
            parked = set(parked_ready_nodes(view))
            # Under `!` a head's expansion is not a filter: every node row is
            # built and the ones waiting on a human are listed with their
            # head, so a live prompt or an exit to close beneath a collapsed
            # head stays discoverable. Attempt rows still follow their node's
            # expansion (the node row already carries its latest attempt's
            # ask), and the normal tree keeps the collapse exactly as before.
            if self.attention_only or ("initiative", initiative_id) in self.expanded:
                for node in sorted(view.get("nodes", []), key=lambda item: item["node_id"]):
                    attempt = _latest_attempt(view, node["node_id"])
                    worker, worker_attention, task_id, observed = _attempt_worker(attempt, by_attempt, task_index)
                    node_row = InitiativeRow(
                        "node", 1, node["node_id"], initiative_id,
                        node.get("goal", node["node_id"]), node.get("state", "?"),
                        node.get("type", "?"),
                        attention=_node_attention(
                            initiative.get("state"), node.get("state"),
                            node["node_id"] in parked, worker_attention,
                            directives.get(node["node_id"], "-"),
                        ),
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
            if candidates:
                shown += 1
            rows.extend(candidates)
        self.retained_counts = {
            "shown": shown, "loaded": loaded, "hidden": max(0, loaded - shown),
            "partial": partial, "unknown": unknown,
        }
        # Archived heads are loaded as head metadata only, so their link
        # records are not read: their workers cannot be proved bound here.
        self.binding_complete = not partial and not any(
            view.get("_metadata_only") for view in self.views
        )
        rows.extend(self._task_branch(bound, needle))
        return rows

    def _room_branch(self, needle: str) -> list[InitiativeRow]:
        """Persistent project conversations under one expanded root."""
        if not self.room_rows:
            return []
        head = InitiativeRow(
            "rooms-root", 0, "rooms", "rooms", "Rooms",
            str(len(self.room_rows)), "rooms",
        )
        children: list[InitiativeRow] = []
        if ("rooms-root", "rooms") in self.expanded:
            for room in sorted(
                self.room_rows,
                key=lambda item: (item.get("state") != "open", item.get("name", "").casefold()),
            ):
                children.append(InitiativeRow(
                    "room", 1, room["room_id"], "rooms",
                    room.get("name", room["room_id"][:8]), room.get("state", "?"),
                    room.get("harness", "?"), coordinator=room.get("project_name", "?"),
                    attention=(
                        "shared working tree" if room.get("shared_working_tree") else "-"
                    ),
                    worker=room.get("harness", "?"),
                    observed_at=room.get("updated_at"),
                ))
        return self._narrow([head, *children], head, needle)

    def _narrow(
        self, candidates: list[InitiativeRow], head: InitiativeRow, needle: str,
    ) -> list[InitiativeRow]:
        """Apply the text filter and the waiting-on-me filter; keep a matching head.

        With `attention_only` the candidates hold every node row whether or
        not the head is expanded, so a head whose only demand sits on a node
        row is kept for that row; a head with nothing waiting beneath it (a
        parked idle initiative, an armed coordinator) still leaves.
        """
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
        flat = not self.views and not self.room_rows
        head = InitiativeRow(
            "tasks-root", 0, "unbound", "unbound",
            # Honest denominator: with a retained head whose links were never
            # read, or a truncated read, "unbound" is what we could not bind,
            # not what is proved unowned.
            "Unbound tasks" if self.binding_complete else "Unbound tasks (binding partial)",
            str(len(unbound)), "tasks",
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

    @property
    def selected_room(self) -> dict[str, Any] | None:
        row = self.selected_row
        if row is None or row.kind != "room":
            return None
        return next(
            (copy.deepcopy(room) for room in self.room_rows if room["room_id"] == row.id),
            None,
        )

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
        if row is None or row.kind in {"attempt", "task", "room"}:
            return False
        if row.kind == "rooms-root":
            key = ("rooms-root", "rooms")
        elif row.kind == "tasks-root":
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
        if row.kind == "rooms-root":
            key = ("rooms-root", "rooms")
        elif row.kind == "initiative":
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

    def set_view_scope(self, value: str) -> bool:
        """Switch the session-local retained view; selection survives by identity."""
        if value not in RETAINED_VIEW_SCOPES:
            raise ValueError("view_scope must be current or all")
        if value == self.view_scope:
            return False
        selected = None if self.selected_row is None else self.selected_row.key
        self.view_scope = value
        rows = self.rows()
        self.selection = next(
            (index for index, row in enumerate(rows) if row.key == selected),
            0 if rows else None,
        )
        self.scroll_offset = 0
        self._clamp_selection()
        return True

    def toggle_view_scope(self) -> str:
        """Flip Current <-> All retained and return the new scope."""
        self.set_view_scope("all" if self.view_scope == "current" else "current")
        return self.view_scope

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
        room_rows: Iterable[dict[str, Any]] | None = None,
    ) -> None:
        selected = None if self.selected_row is None else self.selected_row.key
        self.views = [copy.deepcopy(view) for view in views]
        if task_rows is not None:
            self.task_rows = tuple(task_rows)
        if room_rows is not None:
            self.room_rows = copy.deepcopy(list(room_rows))
        self._data_revision += 1
        rows = self.rows()
        self.selection = next(
            (index for index, row in enumerate(rows) if row.key == selected),
            0 if rows else None,
        )
        self._clamp_selection()

    def apply_refresh(
        self,
        *,
        views: Iterable[dict[str, Any]] | None = None,
        task_rows: Iterable[Any] | None = None,
        room_rows: Iterable[dict[str, Any]] | None = None,
        changed_task_rows: Iterable[Any] | None = None,
        removed_task_ids: Iterable[str] = (),
        task_row_order_changed: bool = True,
    ) -> None:
        """Apply only worker-precomputed branches that actually changed."""
        selected = None if self.selected_row is None else self.selected_row.key
        prior_key = self._current_rows_key()
        changed_by_id = {
            getattr(row, "task", {}).get("task_id"): row
            for row in (() if changed_task_rows is None else changed_task_rows)
        }
        can_patch_tasks = bool(
            task_rows is not None
            and views is None
            and room_rows is None
            and changed_task_rows is not None
            and not tuple(removed_task_ids)
            and not task_row_order_changed
            and not self.filter_string
            and not self.attention_only
            and self._rows_cache is not None
            and self._rows_cache_key == prior_key
        )
        if views is not None:
            self.views = [copy.deepcopy(view) for view in views]
        if task_rows is not None:
            # TuiRow is a frozen, detached worker handoff. Reusing this tuple
            # preserves object identity for every unchanged visible task.
            self.task_rows = tuple(task_rows)
        if room_rows is not None:
            self.room_rows = copy.deepcopy(list(room_rows))
        self._data_revision += 1
        bound, by_attempt = _link_maps(self.views)
        task_index = {
            getattr(row, "task", {}).get("task_id"): row for row in self.task_rows
        }
        if can_patch_tasks:
            # A task-only patch can still change which heads belong in the
            # current view: a worker that reaches a prompt makes a quiet head
            # current, and answering it makes it quiet again. Reclassify only
            # the heads whose tasks moved; if any crosses the boundary the
            # patch cannot express it and the tree rebuilds instead.
            shown_heads = {
                row.initiative_id for row in (self._rows_cache or ())
                if row.kind == "initiative"
            }
            affected = {
                bound[task_id][0] for task_id in changed_by_id
                if task_id in bound
            }
            for initiative_id in affected:
                view = self.view_for(initiative_id)
                if view is None:
                    can_patch_tasks = False
                    break
                _demand, verdict = self._classify(view, by_attempt, task_index)
                if self._in_scope(verdict) != (initiative_id in shown_heads):
                    can_patch_tasks = False
                    break
        if can_patch_tasks:
            directive_labels: dict[str, dict[str, str]] = {}
            patched: list[InitiativeRow] = []
            for visible in self._rows_cache or ():
                task_row = changed_by_id.get(visible.task_id)
                if task_row is None:
                    patched.append(visible)
                    continue
                state = getattr(task_row, "display_state", "?")
                attention = _task_attention(task_row)
                view = (
                    self.view_for(visible.initiative_id)
                    if visible.kind in {"node", "attempt"} else None
                )
                if view is not None and state == "idle":
                    attempt = None
                    if visible.kind == "attempt":
                        attempt = next(
                            (item for item in view.get("attempts", [])
                             if item["attempt_id"] == visible.id),
                            None,
                        )
                    else:
                        attempt = _latest_attempt(view, visible.id)
                    if attempt is not None and attempt.get("state") in {
                        "reported", "awaiting-exit",
                    }:
                        attention = "awaiting exit (X closes)"
                if visible.kind == "node":
                    # The views did not change on this patch, so the node's
                    # own state and the parked-coordinator verdict computed
                    # at build time still hold; only the worker fact moved.
                    # A directive's classification reads that same worker
                    # fact, so it is recomputed here rather than carried.
                    if view is not None and visible.initiative_id not in directive_labels:
                        directive_labels[visible.initiative_id] = {
                            item["node_id"]: "directive pending"
                            for item in initiative_demand(
                                view, by_attempt=by_attempt, task_index=task_index,
                            )
                            if item["kind"] == "directive-pending" and item.get("node_id")
                        }
                    attention = _node_attention(
                        None if view is None else view["initiative"].get("state"),
                        visible.state, visible.attention == "coordinator parked",
                        attention,
                        directive_labels.get(visible.initiative_id, {}).get(visible.id, "-"),
                    )
                observed = getattr(
                    getattr(task_row, "observation", None), "observed_at", None,
                )
                task = getattr(task_row, "task", {})
                patched.append(replace(
                    visible,
                    label=(
                        getattr(task_row, "summary", {}).get(
                            "slug", task.get("task_id", "?"),
                        )
                        if visible.kind == "task" else visible.label
                    ),
                    state=state if visible.kind == "task" else visible.state,
                    type=(
                        (task.get("runs") or [{}])[0].get("harness", "task")
                        if visible.kind == "task" else visible.type
                    ),
                    attention=attention,
                    worker=state,
                    observed_at=observed,
                ))
            self._rows_cache = tuple(patched)
            self._rows_cache_key = self._current_rows_key()
            self._recount(patched, by_attempt, task_index)
        rows = self.rows()
        self.selection = next(
            (index for index, row in enumerate(rows) if row.key == selected),
            0 if rows else None,
        )
        self._clamp_selection()

    # -- facts --------------------------------------------------------------

    def detail_lines(self) -> list[str]:
        room = self.selected_room
        if room is not None:
            return [
                f"{room.get('name', '?')}  [{room.get('state', '?')}]  {room.get('harness', '?')}",
                f"Project: {room.get('project_name', '?')}  {room.get('project_root', '?')}",
                f"Room ID: {room.get('room_id', '?')}",
                f"Tmux: {room.get('session', '?')}  pane {room.get('pane_id') or '-'}",
                f"Shared checkout: {'yes' if room.get('shared_working_tree') else 'no'}",
                f"Evidence: {room.get('detail', '?')}",
            ]
        view = self.selected_view
        if view is None:
            return ["No initiative is selected."]
        initiative = view["initiative"]
        if view.get("_metadata_only"):
            # Bounded archived head metadata: the graph beneath it was never
            # read, so nothing here may claim what its nodes or attempts did.
            return [
                f"{initiative.get('slug', '?')}  [{initiative.get('state', '?')}]  "
                f"{initiative.get('label', '')}",
                f"Initiative: {initiative.get('initiative_id', '?')}",
                f"Updated:    {initiative.get('updated_at', '?')}",
                "History:    archived head metadata only; nodes, attempts, events, "
                "seals and links were not loaded.",
                "Read:       `asha initiative show` reads the archived graph "
                "outside this bounded refresh.",
            ]
        if view.get("_graph_unavailable"):
            # The head record read; its graph did not. Every line the normal
            # pane draws would be an assertion over records nobody opened, so
            # the pane names the failure and claims nothing else.
            return [
                f"{initiative.get('slug', '?')}  [{initiative.get('state', '?')}]  "
                f"{initiative.get('label', '')}",
                f"Initiative: {initiative.get('initiative_id', '?')}",
                f"Updated:    {initiative.get('updated_at', '?')}",
                "Records:    unknown; this head's nodes, attempts, events, seals "
                "and links could not be read in this refresh.",
                f"Reason:     {view.get('_graph_error', 'not recorded')}",
                "Read:       `asha initiative show` reads the graph outside "
                "this bounded refresh.",
            ]
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


__all__ = [
    "InitiativeRow", "InitiativeTreeModel", "InitiativesScreen", "TuiModel",
    "RETAINED_VIEW_SCOPES", "attention_items", "initiative_demand",
    "retained_classification",
]

# The unified control tree is this screen; the alias names the role.
ControlTree = InitiativesScreen
