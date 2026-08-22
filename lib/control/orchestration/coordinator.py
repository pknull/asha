"""Coordinator claims: Asha's own session binds one generation to its tmux pane.

The controller never launches a coordinator. The operator's Asha session runs
``asha initiative coordinator claim`` from inside a tmux pane; that pane's pid
and process identity become the generation's anchor, and every later
coordinator-actor verb proves it still runs inside that pane. A newer claim
fences the previous live or stale generation. Operator approval verbs refuse
the coordinator actor and the coordinator's pane so the authority split stays
structural rather than prompt-enforced.
"""

from __future__ import annotations

import copy
import time
from datetime import datetime, timezone
from typing import Any, Mapping

from ..harness import (
    HarnessError,
    caller_descends_from,
    process_identity,
    validate_harness,
    verify_process,
)
from ..tmux import TmuxAdapter, TmuxError
from .actions import append_event
from .model import (
    COORDINATOR_CONTRACT,
    COORDINATOR_LIVE_STATES,
    COORDINATOR_PROTOCOL_VERSION,
    ModelError,
    new_uuid,
    record_digest,
    validate_coordinator,
)
from .store import InitiativeStore

WAIT_CONTRACT = "asha.orchestration-event-wait.v1"
COORDINATOR_SHOW_CONTRACT = "asha.orchestration-coordinator-show.v1"
ENV_INITIATIVE_ID = "ASHA_ORCHESTRATION_INITIATIVE_ID"
ENV_COORDINATOR_ID = "ASHA_ORCHESTRATION_COORDINATOR_ID"
ENV_GENERATION = "ASHA_ORCHESTRATION_COORDINATOR_GENERATION"
PANE_COORDINATOR_OPTION = "@asha_coordinator_id"
PANE_INITIATIVE_OPTION = "@asha_initiative_id"
PANE_GENERATION_OPTION = "@asha_generation"
_WAIT_TICK_SECONDS = 0.25


class CoordinatorError(ValueError):
    """A coordinator verb was refused before any effect."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def actor_id(record: Mapping[str, Any]) -> str:
    return f"coordinator:{record['coordinator_id']}"


def environment_for(record: Mapping[str, Any]) -> dict[str, str]:
    """Identifiers the claiming session exports; they select records, never authorize."""
    return {
        ENV_INITIATIVE_ID: record["initiative_id"],
        ENV_COORDINATOR_ID: record["coordinator_id"],
        ENV_GENERATION: str(record["generation"]),
    }


def caller_anchor(env: Mapping[str, str], tmux: TmuxAdapter) -> dict[str, Any]:
    """Resolve the calling pane and prove this process descends from it."""
    pane = env.get("TMUX_PANE")
    if not pane:
        raise CoordinatorError("coordinator verbs run inside a tmux pane; TMUX_PANE is unset")
    try:
        facts = tmux.pane_facts(pane)
        server_pid = tmux.server_pid()
    except TmuxError as exc:
        raise CoordinatorError(f"calling tmux pane is unavailable: {exc}") from exc
    if facts.dead or facts.pane_pid is None:
        raise CoordinatorError("calling tmux pane has no live process")
    try:
        identity = process_identity(facts.pane_pid)
    except HarnessError as exc:
        raise CoordinatorError(str(exc)) from exc
    if identity is None:
        raise CoordinatorError("calling tmux pane process is gone")
    if not caller_descends_from(facts.pane_pid):
        raise CoordinatorError("caller does not descend from its tmux pane process")
    return {
        "tmux_socket": tmux.socket,
        "session": facts.session,
        "pane_id": facts.pane_id,
        "pane_pid": facts.pane_pid,
        "process_start_identity": identity,
        "server_pid": server_pid,
    }


def anchor_liveness(anchor: Mapping[str, Any], tmux: TmuxAdapter) -> tuple[bool, str]:
    """Is the recorded pane still the same live process?"""
    try:
        facts = tmux.pane_facts(anchor["pane_id"])
    except TmuxError as exc:
        return False, f"anchor pane unavailable: {exc}"
    if facts.dead or facts.pane_pid != anchor["pane_pid"] or facts.session != anchor["session"]:
        return False, "anchor pane identity changed"
    try:
        if not verify_process(anchor["pane_pid"], anchor["process_start_identity"]):
            return False, "anchor process identity changed"
    except HarnessError as exc:
        return False, str(exc)
    return True, "anchor live"


def require_anchored_caller(
    record: Mapping[str, Any], env: Mapping[str, str], tmux: TmuxAdapter,
) -> None:
    """Every coordinator-actor verb: live generation, live anchor, caller inside it."""
    if record["state"] not in COORDINATOR_LIVE_STATES:
        raise CoordinatorError(
            f"coordinator generation {record['generation']} is {record['state']}"
        )
    if env.get("TMUX_PANE") != record["anchor"]["pane_id"]:
        raise CoordinatorError("caller is not inside the coordinator's anchor pane")
    live, detail = anchor_liveness(record["anchor"], tmux)
    if not live:
        raise CoordinatorError(detail)
    if not caller_descends_from(record["anchor"]["pane_pid"]):
        raise CoordinatorError("caller does not descend from the coordinator's anchor process")
    for key, field in ((ENV_INITIATIVE_ID, "initiative_id"), (ENV_COORDINATOR_ID, "coordinator_id")):
        value = env.get(key)
        if value is not None and value != record[field]:
            raise CoordinatorError(f"{key} does not select this coordinator")
    generation = env.get(ENV_GENERATION)
    if generation is not None and generation != str(record["generation"]):
        raise CoordinatorError(f"{ENV_GENERATION} does not select this generation")


def current_live_coordinator(store: InitiativeStore, initiative_id: str) -> dict[str, Any] | None:
    current = store.current_coordinator(initiative_id)
    if current is None or current["state"] not in COORDINATOR_LIVE_STATES:
        return None
    return current


def require_live_coordinator(store: InitiativeStore, initiative_id: str) -> dict[str, Any]:
    current = current_live_coordinator(store, initiative_id)
    if current is None:
        raise CoordinatorError("no live coordinator generation; run coordinator claim first")
    return current


def refuse_coordinator_pane(
    store: InitiativeStore, initiative_id: str, env: Mapping[str, str], tmux: TmuxAdapter,
) -> None:
    """Operator approval verbs refuse the coordinator's own session and pane."""
    if env.get(ENV_COORDINATOR_ID):
        raise CoordinatorError(
            "approval verbs are refused inside a coordinator session; approve from your own terminal"
        )
    current = current_live_coordinator(store, initiative_id)
    if current is None:
        return
    pane = env.get("TMUX_PANE")
    if pane and pane == current["anchor"]["pane_id"]:
        raise CoordinatorError(
            "approval verbs are refused from the coordinator's pane; approve from your own terminal"
        )


def _mark_pane(tmux: TmuxAdapter, record: Mapping[str, Any]) -> None:
    pane = record["anchor"]["pane_id"]
    try:
        tmux.set_pane_option(pane, PANE_COORDINATOR_OPTION, record["coordinator_id"])
        tmux.set_pane_option(pane, PANE_INITIATIVE_OPTION, record["initiative_id"])
        tmux.set_pane_option(pane, PANE_GENERATION_OPTION, str(record["generation"]))
    except TmuxError as exc:
        raise CoordinatorError(
            f"claimed generation {record['generation']}, but pane marking failed: {exc}"
        ) from exc


def _clear_pane(tmux: TmuxAdapter, pane: str) -> None:
    try:
        for option in (PANE_COORDINATOR_OPTION, PANE_INITIATIVE_OPTION, PANE_GENERATION_OPTION):
            tmux.set_pane_option(pane, option, "")
    except TmuxError:
        # The record is already terminal; a stale marker on a released pane
        # grants nothing because approval refusal binds to the live anchor.
        return


def _fence(store: InitiativeStore, initiative_id: str, current: dict[str, Any], at: str, successor: int) -> None:
    fenced = copy.deepcopy(current)
    fenced.update({"state": "fenced", "updated_at": at})
    store.save_coordinator(initiative_id, fenced, expected_digest=record_digest(current))
    append_event(
        store, initiative_id, "coordinator-generation-fenced", [current["coordinator_id"]],
        {"generation": current["generation"], "superseded_by": successor, "reason": "newer claim"},
        actor_kind="controller", actor_id="coordinator-broker",
    )


def claim(
    store: InitiativeStore,
    initiative: Mapping[str, Any],
    *,
    env: Mapping[str, str],
    tmux: TmuxAdapter,
    harness: str | None = None,
) -> dict[str, Any]:
    """Claim the next coordinator generation from the calling pane (idempotent per pane)."""
    initiative_id = initiative["initiative_id"]
    anchor = caller_anchor(env, tmux)
    try:
        name = validate_harness(
            harness or env.get("ASHA_HARNESS") or store.config.default_coordinator_harness
        )
    except HarnessError as exc:
        raise CoordinatorError(str(exc)) from exc
    at = _now()
    with store.transaction_lock(initiative_id):
        current = store.current_coordinator(initiative_id)
        if (
            current is not None
            and current["state"] in COORDINATOR_LIVE_STATES
            and current["anchor"] == anchor
        ):
            record = current
        else:
            predecessor = None
            generation = 1
            if current is not None:
                predecessor = current["coordinator_id"]
                generation = current["generation"] + 1
                if current["state"] in COORDINATOR_LIVE_STATES or current["state"] == "stale":
                    _fence(store, initiative_id, current, at, generation)
            tail = store.peek(initiative_id)["last_event_sequence"]
            record = {
                "contract": COORDINATOR_CONTRACT,
                "initiative_id": initiative_id,
                "coordinator_id": new_uuid(),
                "generation": generation,
                "state": "active",
                "harness": name,
                "anchor": anchor,
                "protocol_version": COORDINATOR_PROTOCOL_VERSION,
                "claimed_at": at,
                "event_cursor": tail,
                "last_accepted_action_id": None,
                "predecessor_coordinator_id": predecessor,
                "created_at": at,
                "updated_at": at,
            }
            try:
                validate_coordinator(record)
            except ModelError as exc:
                raise CoordinatorError(str(exc)) from exc
            store.save_coordinator(initiative_id, record)
            append_event(
                store, initiative_id, "coordinator-handshake-accepted",
                [record["coordinator_id"]],
                {
                    "generation": generation, "harness": name,
                    "pane_id": anchor["pane_id"], "event_cursor": tail,
                },
                actor_kind="coordinator", actor_id=actor_id(record),
            )
    _mark_pane(tmux, record)
    return record


def release(
    store: InitiativeStore,
    initiative: Mapping[str, Any],
    *,
    env: Mapping[str, str],
    tmux: TmuxAdapter,
) -> dict[str, Any]:
    """The anchored caller retires its live generation: active -> stopping -> exited."""
    initiative_id = initiative["initiative_id"]
    with store.transaction_lock(initiative_id):
        current = require_live_coordinator(store, initiative_id)
        require_anchored_caller(current, env, tmux)
        at = _now()
        stopping = copy.deepcopy(current)
        stopping.update({"state": "stopping", "updated_at": at})
        store.save_coordinator(initiative_id, stopping, expected_digest=record_digest(current))
        exited = copy.deepcopy(stopping)
        exited["state"] = "exited"
        store.save_coordinator(initiative_id, exited, expected_digest=record_digest(stopping))
    _clear_pane(tmux, current["anchor"]["pane_id"])
    return exited


def show(
    store: InitiativeStore, initiative: Mapping[str, Any], *, tmux: TmuxAdapter,
) -> dict[str, Any]:
    """Current generation, its anchor liveness, and the generation history."""
    initiative_id = initiative["initiative_id"]
    current = store.current_coordinator(initiative_id)
    if current is None:
        live, detail = False, "no coordinator has claimed this initiative"
    elif current["state"] not in COORDINATOR_LIVE_STATES:
        live, detail = False, f"generation {current['generation']} is {current['state']}"
    else:
        live, detail = anchor_liveness(current["anchor"], tmux)
    return {
        "contract": COORDINATOR_SHOW_CONTRACT,
        "initiative_id": initiative_id,
        "coordinator": current,
        "anchor_live": live,
        "anchor_detail": detail,
        "generations": [
            {
                "coordinator_id": item["coordinator_id"],
                "generation": item["generation"],
                "state": item["state"],
                "claimed_at": item["claimed_at"],
            }
            for item in store.list_coordinators_snapshot(initiative_id)
        ],
    }


def wait(
    store: InitiativeStore,
    initiative: Mapping[str, Any],
    *,
    env: Mapping[str, str],
    tmux: TmuxAdapter,
    after: int,
    timeout: float,
) -> dict[str, Any]:
    """Bounded lock-free poll for events after a cursor; advances the durable cursor."""
    initiative_id = initiative["initiative_id"]
    current = require_live_coordinator(store, initiative_id)
    require_anchored_caller(current, env, tmux)
    if timeout < 0:
        raise CoordinatorError("wait timeout must be non-negative")
    tail = store.peek(initiative_id)["last_event_sequence"]
    if after < 0 or after > tail:
        raise CoordinatorError(f"cursor {after} is outside the durable event tail {tail}")
    budget = min(float(timeout), float(store.config.coordinator_wait_seconds))
    deadline = time.monotonic() + budget
    events = store.list_events_snapshot(initiative_id, after=after)
    while not events:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(_WAIT_TICK_SECONDS, remaining))
        events = store.list_events_snapshot(initiative_id, after=after)
    if events:
        _advance_cursor(store, initiative_id, current, events[-1]["sequence"])
    head = store.peek(initiative_id)
    return {
        "contract": WAIT_CONTRACT,
        "initiative_id": initiative_id,
        "coordinator_id": current["coordinator_id"],
        "generation": current["generation"],
        "after": after,
        "events": events,
        "last_event_sequence": head["last_event_sequence"],
        "state_revision": head["state_revision"],
        "timed_out": not events,
    }


def reconcile_coordinator(
    store: InitiativeStore, initiative_id: str, *, tmux: TmuxAdapter,
) -> dict[str, Any] | None:
    """Mark a live generation stale when its anchor pane or process is gone.

    Stale is not fenced: no new dispatch happens until a new claim fences it,
    and workers already running are left alone (proposal "Coordinator failure").
    """
    with store.transaction_lock(initiative_id):
        current = store.current_coordinator(initiative_id)
        if current is None:
            return None
        if current["state"] not in COORDINATOR_LIVE_STATES:
            return {
                "coordinator_id": current["coordinator_id"], "generation": current["generation"],
                "state": current["state"], "anchor_live": False,
                "detail": f"generation {current['generation']} is {current['state']}",
            }
        live, detail = anchor_liveness(current["anchor"], tmux)
        if live:
            return {
                "coordinator_id": current["coordinator_id"], "generation": current["generation"],
                "state": current["state"], "anchor_live": True, "detail": detail,
            }
        stale = copy.deepcopy(current)
        stale.update({"state": "stale", "updated_at": _now()})
        store.save_coordinator(initiative_id, stale, expected_digest=record_digest(current))
        append_event(
            store, initiative_id, "reconciliation-conflict", [current["coordinator_id"]],
            {
                "subject": "coordinator", "generation": current["generation"],
                "from": current["state"], "to": "stale", "detail": detail,
            },
            actor_kind="controller", actor_id="coordinator-broker",
        )
        return {
            "coordinator_id": current["coordinator_id"], "generation": current["generation"],
            "state": "stale", "anchor_live": False, "detail": detail,
        }


def _advance_cursor(
    store: InitiativeStore, initiative_id: str, current: Mapping[str, Any], newest: int,
) -> None:
    with store.transaction_lock(initiative_id):
        latest = store.read_coordinator(initiative_id, current["coordinator_id"])
        if latest["state"] not in COORDINATOR_LIVE_STATES:
            raise CoordinatorError(
                f"coordinator generation {latest['generation']} is {latest['state']}"
            )
        if newest > latest["event_cursor"]:
            advanced = copy.deepcopy(latest)
            advanced.update({"event_cursor": newest, "updated_at": _now()})
            store.save_coordinator(initiative_id, advanced, expected_digest=record_digest(latest))


__all__ = [
    "COORDINATOR_SHOW_CONTRACT", "WAIT_CONTRACT", "CoordinatorError", "actor_id",
    "anchor_liveness", "caller_anchor", "claim", "current_live_coordinator",
    "environment_for", "reconcile_coordinator", "refuse_coordinator_pane", "release",
    "require_anchored_caller", "require_live_coordinator", "show", "wait",
]
