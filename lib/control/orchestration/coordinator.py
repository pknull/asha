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
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from ..harness import (
    HarnessError,
    caller_descends_from,
    process_identity,
    validate_harness,
    verify_process,
)
from ..tmux import TmuxAdapter, TmuxError
from ..harness import launch_argv
import uuid
from pathlib import Path
from .actions import append_event
from .model import (
    COORDINATOR_CHECKPOINT_CONTRACT,
    COORDINATOR_CONTRACT,
    COORDINATOR_LIVE_STATES,
    COORDINATOR_PROTOCOL_VERSION,
    INITIATIVE_TERMINAL_STATES,
    ModelError,
    checkpoint_digest,
    new_uuid,
    record_digest,
    validate_coordinator,
    validate_coordinator_checkpoint,
)
from .store import InitiativeStore, StoreError

WAIT_CONTRACT = "asha.orchestration-event-wait.v1"
COORDINATOR_SHOW_CONTRACT = "asha.orchestration-coordinator-show.v1"
ENV_INITIATIVE_ID = "ASHA_ORCHESTRATION_INITIATIVE_ID"
ENV_COORDINATOR_ID = "ASHA_ORCHESTRATION_COORDINATOR_ID"
ENV_GENERATION = "ASHA_ORCHESTRATION_COORDINATOR_GENERATION"
PANE_COORDINATOR_OPTION = "@asha_coordinator_id"
PANE_INITIATIVE_OPTION = "@asha_initiative_id"
PANE_GENERATION_OPTION = "@asha_generation"
MAX_COORDINATOR_WAIT_SECONDS = 3600
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
    try:
        server_identity = process_identity(server_pid)
    except HarnessError as exc:
        raise CoordinatorError(str(exc)) from exc
    if server_identity is None:
        raise CoordinatorError("tmux server process is gone")
    tmux_env = env.get("TMUX") or ""
    socket_path = tmux_env.split(",")[0] if tmux_env else None
    return {
        "tmux_socket": socket_path or tmux.socket,
        "session": facts.session,
        "pane_id": facts.pane_id,
        "pane_pid": facts.pane_pid,
        "process_start_identity": identity,
        "server_pid": server_pid,
        "server_start_identity": server_identity,
    }


def anchor_liveness(anchor: Mapping[str, Any], tmux: TmuxAdapter) -> tuple[str, str]:
    """Judge the recorded pane: ``live``, ``gone``, or ``unknown``.

    ``gone`` also covers a dead anchor server (its recorded process identity no
    longer exists). ``unknown`` means the anchor server is alive but the
    caller's tmux server is a different one, so nothing can be said about the
    pane; reconciliation must not mark a generation stale on ``unknown``. The session name is cosmetic (renames and
    moves do not change identity); pane id, pane pid, and the process start
    identity do.
    """
    try:
        anchor_server_alive = verify_process(anchor["server_pid"], anchor["server_start_identity"])
    except HarnessError:
        anchor_server_alive = False
    if not anchor_server_alive:
        return "gone", "anchor tmux server is gone"
    try:
        server_pid = tmux.server_pid()
    except TmuxError as exc:
        return "unknown", f"caller tmux server unavailable: {exc}"
    if server_pid != anchor["server_pid"]:
        return "unknown", "caller tmux server differs from the anchor server"
    try:
        facts = tmux.pane_facts(anchor["pane_id"])
    except TmuxError as exc:
        return "gone", f"anchor pane unavailable: {exc}"
    if facts.dead or facts.pane_pid != anchor["pane_pid"]:
        return "gone", "anchor pane identity changed"
    try:
        if not verify_process(anchor["pane_pid"], anchor["process_start_identity"]):
            return "gone", "anchor process identity changed"
    except HarnessError as exc:
        return "gone", str(exc)
    return "live", "anchor live"


def _anchor_key(anchor: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        anchor["server_pid"], anchor["server_start_identity"], anchor["pane_id"],
        anchor["pane_pid"], anchor["process_start_identity"],
    )


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
    state, detail = anchor_liveness(record["anchor"], tmux)
    if state != "live":
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
    """Operator verbs refuse the coordinator's own session and pane.

    Every operator-actor write (approval, plan proposal, convenience actions,
    operator action documents) passes through here, so the coordinator pane can
    act only as the coordinator actor and the journal never attributes a
    coordinator-pane act to the operator.
    """
    if env.get(ENV_COORDINATOR_ID):
        raise CoordinatorError(
            "operator verbs are refused inside a coordinator session; act as the coordinator "
            "or use your own terminal"
        )
    current = current_live_coordinator(store, initiative_id)
    if current is None:
        return
    pane = env.get("TMUX_PANE")
    if pane and pane == current["anchor"]["pane_id"]:
        raise CoordinatorError(
            "operator verbs are refused from the coordinator's pane; act as the coordinator "
            "or use your own terminal"
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


def _reap_pane(tmux: TmuxAdapter, pane: str) -> None:
    """Kill exactly the proven anchor pane through the tmux adapter seam."""
    try:
        tmux._run(["kill-pane", "-t", pane])
    except TmuxError as exc:
        raise CoordinatorError(f"coordinator pane reap failed: {exc}") from exc


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
    with store.transaction_lock(initiative_id):
        at = _now()
        current = store.current_coordinator(initiative_id)
        if (
            current is not None
            and current["state"] in COORDINATOR_LIVE_STATES
            and _anchor_key(current["anchor"]) == _anchor_key(anchor)
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
                "armed_watch": None,
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


def release_with_details(
    store: InitiativeStore,
    initiative: Mapping[str, Any],
    *,
    env: Mapping[str, str],
    tmux: TmuxAdapter,
) -> tuple[dict[str, Any], str | None]:
    """Retire a live generation, with a terminal-only operator reap path.

    A non-terminal initiative still requires the anchored coordinator process.
    Once the initiative itself is terminal, an operator outside that pane may
    retire the generation and reap its exact anchor pane.  The returned
    pane ID is transient reporting data; it is never added to the coordinator
    record.
    """
    initiative_id = initiative["initiative_id"]
    current = require_live_coordinator(store, initiative_id)
    selected_coordinator_id = current["coordinator_id"]
    selected_generation = current["generation"]
    caller_is_anchor = env.get("TMUX_PANE") == current["anchor"]["pane_id"]
    terminal = store.peek(initiative_id)["state"] in INITIATIVE_TERMINAL_STATES
    if caller_is_anchor or not terminal:
        require_anchored_caller(current, env, tmux)
        with store.transaction_lock(initiative_id):
            current = require_live_coordinator(store, initiative_id)
            require_anchored_caller(current, env, tmux)
            at = _now()
            stopping = copy.deepcopy(current)
            stopping.update({"state": "stopping", "armed_watch": None, "updated_at": at})
            store.save_coordinator(
                initiative_id, stopping, expected_digest=record_digest(current),
            )
            exited = copy.deepcopy(stopping)
            exited["state"] = "exited"
            store.save_coordinator(
                initiative_id, exited, expected_digest=record_digest(stopping),
            )
        _clear_pane(tmux, current["anchor"]["pane_id"])
        return exited, None

    if any(env.get(key) for key in (ENV_INITIATIVE_ID, ENV_COORDINATOR_ID, ENV_GENERATION)):
        raise CoordinatorError(
            "terminal coordinator reap is an operator act; coordinator selectors must be unset"
        )
    liveness, detail = anchor_liveness(current["anchor"], tmux)
    if liveness == "unknown":
        raise CoordinatorError(detail)
    reap_live_pane = False
    if liveness == "live":
        try:
            facts = tmux.pane_facts(current["anchor"]["pane_id"])
        except TmuxError as exc:
            raise CoordinatorError(f"anchor pane unavailable: {exc}") from exc
        if facts.dead or facts.pane_pid != current["anchor"]["pane_pid"]:
            raise CoordinatorError("anchor pane identity changed")
        reap_live_pane = True

    with store.transaction_lock(initiative_id):
        current = require_live_coordinator(store, initiative_id)
        if (
            current["coordinator_id"] != selected_coordinator_id
            or current["generation"] != selected_generation
        ):
            raise CoordinatorError("coordinator generation changed during terminal reap")
        if current["state"] != "stopping":
            stopping = copy.deepcopy(current)
            stopping.update({
                "state": "stopping", "armed_watch": None, "updated_at": _now(),
            })
            store.save_coordinator(
                initiative_id, stopping, expected_digest=record_digest(current),
            )

    reaped_pane_id: str | None = None
    if reap_live_pane:
        _reap_pane(tmux, current["anchor"]["pane_id"])
        reaped_pane_id = current["anchor"]["pane_id"]

    with store.transaction_lock(initiative_id):
        stopping = store.read_coordinator(initiative_id, current["coordinator_id"])
        if stopping["state"] != "stopping":
            raise CoordinatorError("coordinator generation changed during terminal reap")
        exited = copy.deepcopy(stopping)
        exited.update({"state": "exited", "updated_at": _now()})
        store.save_coordinator(
            initiative_id, exited, expected_digest=record_digest(stopping),
        )
    return exited, reaped_pane_id


def release(
    store: InitiativeStore,
    initiative: Mapping[str, Any],
    *,
    env: Mapping[str, str],
    tmux: TmuxAdapter,
) -> dict[str, Any]:
    """Compatibility wrapper returning only the retired coordinator record."""
    record, _reaped_pane_id = release_with_details(
        store, initiative, env=env, tmux=tmux,
    )
    return record


def show(
    store: InitiativeStore, initiative: Mapping[str, Any], *, tmux: TmuxAdapter,
) -> dict[str, Any]:
    """Current generation, its anchor liveness, and the generation history."""
    initiative_id = initiative["initiative_id"]
    current = store.current_coordinator(initiative_id)
    live: bool | None
    if current is None:
        live, detail = False, "no coordinator has claimed this initiative"
    elif current["state"] not in COORDINATOR_LIVE_STATES:
        live, detail = False, f"generation {current['generation']} is {current['state']}"
    else:
        state, detail = anchor_liveness(current["anchor"], tmux)
        live = None if state == "unknown" else state == "live"
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
    """Segmented lock-free poll for events after a cursor; advances the durable cursor."""
    initiative_id = initiative["initiative_id"]
    current = require_live_coordinator(store, initiative_id)
    require_anchored_caller(current, env, tmux)
    if timeout < 0:
        raise CoordinatorError("wait timeout must be non-negative")
    initial = store.peek(initiative_id)
    tail = initial["last_event_sequence"]
    if after < 0 or after > tail:
        raise CoordinatorError(f"cursor {after} is outside the durable event tail {tail}")
    budget = min(float(timeout), float(MAX_COORDINATOR_WAIT_SECONDS))
    segment = float(store.config.coordinator_wait_seconds)
    started = time.monotonic()
    deadline = started + budget
    segment_deadline = min(deadline, started + segment)
    ended = (
        "terminal-initiative"
        if initial["state"] in INITIATIVE_TERMINAL_STATES
        else None
    )
    events = (
        []
        if ended is not None
        else store.list_events_snapshot(initiative_id, after=after)
    )
    armed_watch: dict[str, Any] | None = None
    if not events and ended is None and budget > 0:
        watch_deadline = (
            datetime.now(timezone.utc) + timedelta(seconds=budget)
        ).isoformat(timespec="microseconds").replace("+00:00", "Z")
        current, armed_watch = _arm_watch(
            store, initiative_id, current, after, watch_deadline,
        )
    try:
        while not events and ended is None:
            now = time.monotonic()
            remaining = min(deadline, segment_deadline) - now
            if remaining <= 0:
                if now >= deadline:
                    break
                try:
                    candidate = require_live_coordinator(store, initiative_id)
                    require_anchored_caller(candidate, env, tmux)
                except CoordinatorError:
                    ended = "stale-generation"
                    break
                if (
                    candidate["coordinator_id"] != current["coordinator_id"]
                    or candidate["generation"] != current["generation"]
                ):
                    ended = "stale-generation"
                    break
                current = candidate
                head = store.peek(initiative_id)
                if head["state"] in INITIATIVE_TERMINAL_STATES:
                    ended = "terminal-initiative"
                    break
                segment_deadline = min(deadline, now + segment)
                continue
            time.sleep(min(_WAIT_TICK_SECONDS, remaining))
            events = store.list_events_snapshot(initiative_id, after=after)
    finally:
        newest = None if not events else events[-1]["sequence"]
        if armed_watch is not None:
            _finish_watch(
                store, initiative_id, current, armed_watch, newest=newest,
            )
        elif newest is not None:
            _advance_cursor(store, initiative_id, current, newest)
    head = store.peek(initiative_id)
    payload = {
        "contract": WAIT_CONTRACT,
        "initiative_id": initiative_id,
        "coordinator_id": current["coordinator_id"],
        "generation": current["generation"],
        "after": after,
        "events": events,
        "last_event_sequence": head["last_event_sequence"],
        "state_revision": head["state_revision"],
        "timed_out": not events and ended is None,
    }
    if ended is not None:
        payload["ended"] = ended
    return payload


def checkpoint(
    store: InitiativeStore,
    initiative: Mapping[str, Any],
    *,
    env: Mapping[str, str],
    tmux: TmuxAdapter,
    document: Mapping[str, Any],
) -> dict[str, Any]:
    """Replace this generation's checkpoint under CAS; a hint for re-claims, never authority."""
    initiative_id = initiative["initiative_id"]
    current = require_live_coordinator(store, initiative_id)
    require_anchored_caller(current, env, tmux)
    if not isinstance(document, Mapping):
        raise CoordinatorError("checkpoint document must be an object")
    record = dict(document)
    record.update({
        "contract": COORDINATOR_CHECKPOINT_CONTRACT,
        "initiative_id": initiative_id,
        "coordinator_id": current["coordinator_id"],
        "generation": current["generation"],
        "recorded_at": _now(),
    })
    record["digest"] = checkpoint_digest(record)
    try:
        validate_coordinator_checkpoint(record)
    except ModelError as exc:
        raise CoordinatorError(str(exc)) from exc
    tail = store.peek(initiative_id)["last_event_sequence"]
    if record["event_cursor"] > tail:
        raise CoordinatorError(f"checkpoint cursor {record['event_cursor']} is beyond the durable tail {tail}")
    with store.transaction_lock(initiative_id):
        try:
            previous = store.read_checkpoint(initiative_id, current["coordinator_id"])
        except StoreError as exc:
            if "not found" not in str(exc):
                raise
            previous = None
        prior = None if previous is None else previous["digest"]
        if record["prior_checkpoint_digest"] != prior:
            raise CoordinatorError("checkpoint prior_checkpoint_digest does not match the retained checkpoint")
        store.save_checkpoint(
            initiative_id, record,
            expected_digest=None if previous is None else record_digest(previous),
        )
        append_event(
            store, initiative_id, "coordinator-checkpointed", [current["coordinator_id"]],
            {"generation": current["generation"], "digest": record["digest"], "event_cursor": record["event_cursor"]},
            actor_kind="coordinator", actor_id=actor_id(current),
        )
    return record


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
        state, detail = anchor_liveness(current["anchor"], tmux)
        if state != "gone":
            return {
                "coordinator_id": current["coordinator_id"], "generation": current["generation"],
                "state": current["state"],
                "anchor_live": True if state == "live" else None, "detail": detail,
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


def _arm_watch(
    store: InitiativeStore,
    initiative_id: str,
    current: Mapping[str, Any],
    after: int,
    deadline: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Durably acknowledge the cursor a blocking wait is watching."""
    watch = {"after": after, "deadline": deadline}
    with store.transaction_lock(initiative_id):
        latest = store.read_coordinator(initiative_id, current["coordinator_id"])
        if (
            latest["state"] not in COORDINATOR_LIVE_STATES
            or latest["generation"] != current["generation"]
        ):
            raise CoordinatorError(
                f"coordinator generation {current['generation']} is no longer live"
            )
        existing = latest.get("armed_watch")
        if existing is not None:
            existing_deadline = datetime.fromisoformat(
                existing["deadline"][:-1] + "+00:00"
            )
            if existing_deadline > datetime.now(timezone.utc):
                raise CoordinatorError("coordinator already has an armed event watch")
        armed = copy.deepcopy(latest)
        armed.update({"armed_watch": watch, "updated_at": _now()})
        store.save_coordinator(
            initiative_id, armed, expected_digest=record_digest(latest),
        )
    return armed, watch


def _finish_watch(
    store: InitiativeStore,
    initiative_id: str,
    current: Mapping[str, Any],
    watch: Mapping[str, Any],
    *,
    newest: int | None,
) -> None:
    """Clear this wait's watch and commit any event cursor it observed."""
    with store.transaction_lock(initiative_id):
        latest = store.read_coordinator(initiative_id, current["coordinator_id"])
        if (
            latest["state"] not in COORDINATOR_LIVE_STATES
            or latest["generation"] != current["generation"]
            or latest.get("armed_watch") != dict(watch)
        ):
            return
        finished = copy.deepcopy(latest)
        if newest is not None:
            finished["event_cursor"] = max(finished["event_cursor"], newest)
        finished.update({"armed_watch": None, "updated_at": _now()})
        store.save_coordinator(
            initiative_id, finished, expected_digest=record_digest(latest),
        )


__all__ = [
    "COORDINATOR_SHOW_CONTRACT", "MAX_COORDINATOR_WAIT_SECONDS", "WAIT_CONTRACT",
    "CoordinatorError", "actor_id",
    "anchor_liveness", "caller_anchor", "checkpoint", "claim", "current_live_coordinator",
    "environment_for", "reconcile_coordinator", "refuse_coordinator_pane", "release",
    "release_with_details",
    "require_anchored_caller", "require_live_coordinator", "show", "wait",
]


# ---------------------------------------------------------------------------
# Control-managed coordinator sessions (the monitor's front door)
# ---------------------------------------------------------------------------

COORDINATOR_LAUNCH_CONTRACT = "asha.orchestration-coordinator-launch.v1"
COORDINATOR_SESSIONS_CONTRACT = "asha.orchestration-coordinator-sessions.v1"
COORDINATOR_ATTACH_CONTRACT = "asha.orchestration-coordinator-attach.v1"
COORDINATOR_SESSION_INFIX = "coord-"
MAX_INTENT_BYTES = 2000


def coordinator_session_name(session_prefix: str, token: str) -> str:
    return f"{session_prefix}{COORDINATOR_SESSION_INFIX}{token}"


def launch_prompt(intent: str) -> str:
    """The first message the launched coordinator session receives."""
    return (
        "Use the session-orchestrate-initiative skill for this intent: resolve the "
        "repository with `asha initiative projects`, create and claim the initiative, "
        "propose the plan, and tell the Keeper the digest. Intent: " + intent
    )


def normalize_intent(intent: Any) -> str:
    if not isinstance(intent, str):
        raise CoordinatorError("intent must be text")
    text = " ".join(intent.split())
    if not text:
        raise CoordinatorError("intent must not be empty")
    if len(text.encode("utf-8")) > MAX_INTENT_BYTES:
        raise CoordinatorError(f"intent exceeds {MAX_INTENT_BYTES} UTF-8 bytes")
    return text


def launch_session(
    config: Any, *, root: Path, intent: str, tmux: TmuxAdapter, asha_root: Path,
    harness: str = "claude", token: str | None = None,
) -> dict[str, Any]:
    """Start the coordinator as a Control-owned tmux session at the projects root.

    The session runs the full-persona harness with the intent as its first
    message; the coordinator claims its initiative from that pane like any
    other session. Control never types into the pane afterwards.
    """
    text = normalize_intent(intent)
    directory = Path(root).expanduser().resolve()
    if not directory.is_dir():
        raise CoordinatorError(f"projects root is not a directory: {root}")
    token = token or uuid.uuid4().hex[:8]
    session = coordinator_session_name(config.session_prefix, token)
    argv = launch_argv(asha_root, harness, [launch_prompt(text)])
    pane_id = tmux.create_task_session(
        session=session, window="coordinator", start_directory=directory,
        # Panes inherit the tmux server's env, not the caller's: pass the
        # asha home explicitly so a non-default ASHA_HOME reaches the
        # coordinator's launcher (same rule controller_env applies to workers).
        environment={
            "ASHA_COORDINATOR_LAUNCH": token,
            "ASHA_HOME": str(config.asha_home),
        },
        holder_argv=["sleep", "3600"],
        session_options={"@asha_coordinator_session": "1"},
        pane_options={"@asha_coordinator_launch": token, "@asha_harness": harness},
        pane_title=f"asha:coordinator:{harness}",
    )
    tmux.respawn(pane_id, argv)
    return {
        "contract": COORDINATOR_LAUNCH_CONTRACT,
        "session": session,
        "pane_id": pane_id,
        "root": str(directory),
        "harness": harness,
        "intent": text,
        "launched_at": _now(),
    }


def _claimed_sessions(store: InitiativeStore) -> dict[str, dict[str, Any]]:
    bound: dict[str, dict[str, Any]] = {}
    for initiative in store.list_initiatives():
        try:
            record = store.current_coordinator(initiative["initiative_id"])
        except StoreError:
            # Retained initiatives from before the coordinator layout have no
            # coordinators directory; they were never claimed.
            continue
        if record is None:
            continue
        bound[record["anchor"]["session"]] = {
            "initiative_id": initiative["initiative_id"],
            "slug": initiative["slug"],
            "coordinator_id": record["coordinator_id"],
            "generation": record["generation"],
            "state": record["state"],
        }
    return bound


def list_coordinator_sessions(config: Any, *, store: InitiativeStore, tmux: TmuxAdapter) -> dict[str, Any]:
    """Control-launched coordinator sessions on this server, with their claims when made."""
    prefix = coordinator_session_name(config.session_prefix, "")
    bound = _claimed_sessions(store)
    sessions = []
    for name in sorted(tmux.list_sessions()):
        if not name.startswith(prefix):
            continue
        claim = bound.get(name)
        sessions.append({
            "session": name,
            "initiative_id": None if claim is None else claim["initiative_id"],
            "slug": None if claim is None else claim["slug"],
            "coordinator_id": None if claim is None else claim["coordinator_id"],
            "generation": None if claim is None else claim["generation"],
            "state": None if claim is None else claim["state"],
        })
    return {"contract": COORDINATOR_SESSIONS_CONTRACT, "sessions": sessions}


def attach_target(
    store: InitiativeStore, *, tmux: TmuxAdapter,
    initiative_id: str | None = None, session: str | None = None,
) -> dict[str, Any]:
    """The tmux session to attach to for an initiative's coordinator, or a named session."""
    if (initiative_id is None) == (session is None):
        raise CoordinatorError("attach requires exactly one of an initiative or --session")
    record = None
    if initiative_id is not None:
        record = store.current_coordinator(initiative_id)
        if record is None:
            raise CoordinatorError("no coordinator has claimed this initiative")
        if record["state"] not in COORDINATOR_LIVE_STATES:
            raise CoordinatorError(f"the current coordinator generation is {record['state']}; re-claim or launch a new session")
        session = record["anchor"]["session"]
    if not tmux.has_session(session):
        raise CoordinatorError(f"coordinator session {session} is not running")
    return {
        "contract": COORDINATOR_ATTACH_CONTRACT,
        "initiative_id": initiative_id,
        "session": session,
        "pane_id": None if record is None else record["anchor"]["pane_id"],
        "coordinator_id": None if record is None else record["coordinator_id"],
        "generation": None if record is None else record["generation"],
    }
