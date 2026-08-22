"""Deterministic Asha Control command-line surface."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence, Any

from .config import ConfigError, is_canonical_absolute_path, load_config
from .doctor import run_doctor
from .events import (
    EventError,
    expire_snapshot,
    publish_server_summary,
    read_snapshot,
    write_snapshot,
)
from .jj import (
    ColocationIntentStore, DEFAULT_BASE_REVSET, JjAdapter, JjError, RepositoryFacts,
    colocated_sync_remediation, discover_git_root, inspect_git_marker,
)
from .launch import (
    LaunchError, archive_task, launch_task, recover_task, stop_task,
    unarchive_task,
)
from .model import canonical_uuid, new_uuid, validate_task_slug
from .prepare import (
    PlainGitPreEnablePlan, PrepareRequest, PreparationError,
    PreparationPrerequisiteError,
    preflight_plain_git_enablement, prepare_task_workspace,
    revalidate_plain_git_pre_enable_plan, revalidate_pr_source_proof_after_fetch,
)
from .prerequisites import (
    StartPrerequisiteRefusal, capture_prerequisite_offer,
    encode_worker_refusal,
)
from .prune import (
    PRUNE_CONTRACT, assemble_prune_context, prune_one_task,
)
from .reconcile import LiveAdapters
from .sources import GithubAdapter, SourceError
from .store import StoreError, TaskStore
from .tmux import TmuxAdapter, TmuxError
from .transaction import CreationJournalStore, JournalError
from . import harness as harness_api
from . import view


def _json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _task_usage(stream=sys.stdout) -> None:
    print("""asha task: persistent task workspaces and harness runs

Usage:
  asha task start [--repo PATH] (--pr N | --issue N | [--base REVSET])
                  [--task-id UUID] [--slug SLUG]
                  [--harness H|--agent H] (--goal TEXT | -- TEXT...)
                  [--role ROLE] [--detach] [--json]
  asha task list [--json]
  asha task show <task-id|exact-slug> [--json]
  asha task attach <task-id|exact-slug> [--run RUN_ID]
  asha task stop <task-id|exact-slug> [--terminate]
  asha task archive <task-id|exact-slug>
  asha task unarchive <task-id|exact-slug>
  asha task recover <task-id|exact-slug>
  asha task recover <task-id|exact-slug> --adopt --yes --harness H
                    --role ROLE --goal TEXT
  asha task prune (<task-id|exact-slug>... | --all) [--keep-workspace]
                  [--dry-run] [--yes] [--json]
  asha task reconcile [task-id|exact-slug] [--json]
  asha task report --file RESULT.json [--json]
  asha task result <task-id> [--json]
  asha task seal <task-id|attempt-id> [--json]
  asha task doctor [--json]""", file=stream)


def _control_usage(stream=sys.stdout) -> None:
    print("""asha control: terminal task supervision

Run `asha control` in a terminal to open the Control TUI.
Use `asha task list --json` as the non-interactive fallback.
Use `asha control tmux` to print the optional tmux integration snippet.""", file=stream)


def _parse_event(args: list[str]) -> dict[str, Any]:
    values: dict[str, Any] = {
        "event": None,
        "harness": None,
        "session_id": None,
        "exit_status": None,
        "pane_id": None,
        "json": False,
    }
    seen: set[str] = set()
    index = 0
    while index < len(args):
        argument = args[index]
        if argument == "--json":
            if "json" in seen:
                raise ValueError("--json may be specified only once")
            seen.add("json")
            values["json"] = True
            index += 1
            continue
        destinations = {
            "--event": "event",
            "--harness": "harness",
            "--session-id": "session_id",
            "--exit-status": "exit_status",
            "--pane-id": "pane_id",
        }
        destination = destinations.get(argument)
        if destination is None:
            raise ValueError(f"unknown control event argument: {argument}")
        if destination in seen:
            raise ValueError(f"{argument} may be specified only once")
        if index + 1 >= len(args):
            raise ValueError(f"{argument} requires a value")
        seen.add(destination)
        values[destination] = args[index + 1]
        index += 2
    if values["event"] is None:
        raise ValueError("control event requires --event <name>")
    return values


def _event_diagnostic(exc: BaseException) -> None:
    detail = "".join(
        character if character.isprintable() else "?" for character in str(exc)
    )[:450]
    print(f"asha control event: {detail or 'controller failure'}", file=sys.stderr)


def _event_command(args: list[str], env: Mapping[str, str]) -> int:
    # This is deliberately before parsing or configuration. Ordinary sessions
    # never enter the controller and malformed inherited identity also no-ops.
    task_id = env.get("ASHA_CONTROL_TASK_ID")
    run_id = env.get("ASHA_CONTROL_RUN_ID")
    if env.get("ASHA_CONTROL_MANAGED") != "1" or not task_id or not run_id:
        return 0

    parsed = _parse_event(args)
    try:
        config = load_config(env)
        state_dir = env.get("ASHA_CONTROL_STATE_DIR")
        if (not isinstance(state_dir, str) or
                not is_canonical_absolute_path(state_dir) or
                Path(state_dir) != config.tasks_dir):
            raise EventError(
                "ASHA_CONTROL_STATE_DIR does not match the configured task registry"
            )
        store = TaskStore(config)
        task = store.peek(task_id)
        run = next(
            (candidate for candidate in task["runs"] if candidate["run_id"] == run_id),
            None,
        )
        if run is None:
            raise EventError("submitted run does not belong to the submitted task")
        if task["lifecycle"] in {"ended", "archived"} or run["state"] in {
            "exited", "failed",
        }:
            expire_snapshot(config, run_id)
            publish_server_summary(config, TmuxAdapter())
            return 0
        pane_id = parsed["pane_id"]
        if pane_id is None:
            raise EventError("control event requires --pane-id <pane-id>")
        if pane_id != run["pane_id"]:
            raise EventError("submitted pane does not belong to the submitted run")
        raw_status = parsed["exit_status"]
        exit_status = None
        if raw_status is not None:
            if re.fullmatch(r"[0-9]{1,3}", raw_status) is None:
                raise EventError("exit status must be a decimal integer from 0 through 255")
            exit_status = int(raw_status)
        write_snapshot(
            config,
            task_id=task_id,
            run_id=run_id,
            event=parsed["event"],
            harness=parsed["harness"] or env.get("ASHA_HARNESS") or run["harness"],
            harness_session_id=parsed["session_id"],
            exit_status=exit_status,
            pane_id=pane_id,
        )
        # Event ingestion deliberately does not take the registry lock. Close
        # the race with terminal reconciliation/archive by re-reading after the
        # atomic replace and removing a late write against a terminal record.
        try:
            current = store.peek(task_id)
        except StoreError:
            expire_snapshot(config, run_id)
            raise
        current_run = next(
            (candidate for candidate in current["runs"] if candidate["run_id"] == run_id),
            None,
        )
        if (current_run is None or current["lifecycle"] in {"ended", "archived"} or
                current_run["state"] in {"exited", "failed"}):
            expire_snapshot(config, run_id)
            publish_server_summary(config, TmuxAdapter())
            return 0
        _publish_tmux_presentation(config, run_id, pane_id, task_id)
        if parsed["json"]:
            snapshot = read_snapshot(config, run_id)
            if snapshot is None:
                raise EventError("event snapshot disappeared after its write")
            _json(snapshot)
    except (ConfigError, EventError, StoreError, OSError, ValueError) as exc:
        _event_diagnostic(exc)
    return 0


def _publish_tmux_presentation(
    config, run_id: str, pane_id: str | None, task_id: str,
) -> None:
    """Mirror the new snapshot into the tmux values users bind in their formats.

    The contract's tmux Presentation section requires hooks to update pane-local
    state and the server-level summary; without this, `task show` is correct
    while a status-line binding shows the launch-time value forever.

    Strictly best effort. This runs on the hook hot path, which must never block
    or fail a prompt, tool call, permission response, or session exit, so every
    failure here is swallowed. Ownership is verified before any pane or session
    option is written: a pane whose `@asha_run_id` does not match this run is
    never touched.
    """
    if not pane_id:
        return
    try:
        snapshot = read_snapshot(config, run_id)
        state = snapshot.get("state") if snapshot else None
        adapter = TmuxAdapter()
        if isinstance(state, str):
            if adapter.pane_option(
                pane_id, "@asha_run_id", deadline_seconds=5,
            ) != run_id:
                return
            adapter.set_pane_option(
                pane_id, "@asha_state", state, deadline_seconds=5,
            )
            session = adapter.pane_facts(pane_id, deadline_seconds=5).session
            if adapter.session_option(
                session, "@asha_task_id", deadline_seconds=5,
            ) == task_id:
                adapter.set_session_option(
                    session, "@asha_state", state, deadline_seconds=5,
                )
        publish_server_summary(config, adapter)
    except (EventError, TmuxError, OSError, ValueError):
        return


def _parse_json_flag(args: list[str]) -> tuple[list[str], bool]:
    json_output = False
    remaining: list[str] = []
    for arg in args:
        if arg == "--json":
            if json_output:
                raise ValueError("--json may be specified only once")
            json_output = True
        else:
            remaining.append(arg)
    return remaining, json_output


def _slug(value: str) -> str:
    folded = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    result = re.sub(r"[^a-z0-9]+", "-", folded.casefold()).strip("-")
    result = result[:64].rstrip("-")
    return result or "task"


@dataclass(frozen=True)
class RepositorySelection:
    root: Path
    plain_git: bool


def _repo_argument(value: str | None, config, jj: JjAdapter) -> RepositorySelection:
    if value is None:
        candidate = Path.cwd().resolve()
    else:
        if value == "~":
            candidate = config.home
        elif value.startswith("~/"):
            candidate = config.home / value[2:]
        else:
            candidate = Path(value)
            if not candidate.is_absolute():
                candidate = Path.cwd() / candidate
        candidate = candidate.resolve()
    try:
        return RepositorySelection(jj.discover_root(candidate), plain_git=False)
    except JjError as jj_error:
        git_root = discover_git_root(candidate)
        if git_root is None:
            raise jj_error
        return RepositorySelection(git_root, plain_git=True)


def _parse_start(args: list[str]) -> dict[str, Any]:
    values: dict[str, Any] = {
        "repo": None, "base": DEFAULT_BASE_REVSET, "harness": None, "role": "implementer",
        "goal": None, "pr": None, "issue": None, "task_id": None, "slug": None,
        "expected_default": None, "detach": False, "json": False,
        "tui_worker": False,
    }
    seen: set[str] = set()
    index = 0
    trailing: list[str] | None = None
    while index < len(args):
        argument = args[index]
        if argument == "--":
            trailing = args[index + 1:]
            break
        if argument in {"--detach", "--json", "--tui-worker"}:
            key = argument[2:].replace("-", "_")
            if key in seen:
                raise ValueError(f"{argument} may be specified only once")
            seen.add(key)
            values[key] = True
            index += 1
            continue
        if argument in {"--repo", "--base", "--harness", "--agent", "--goal", "--role",
                        "--pr", "--issue", "--task-id", "--slug",
                        "--expected-default"}:
            if index + 1 >= len(args):
                raise ValueError(f"{argument} requires a value")
            key = argument[2:]
            if key in seen:
                raise ValueError(f"{argument} may be specified only once")
            if key in {"harness", "agent"} and ({"harness", "agent"} & seen):
                raise ValueError("--harness and --agent are mutually exclusive")
            seen.add(key)
            destination = "harness" if key == "agent" else key
            raw_value = args[index + 1]
            if key in {"pr", "issue"}:
                if re.fullmatch(r"[1-9][0-9]{0,9}", raw_value) is None:
                    raise ValueError(f"{argument} requires a positive decimal number")
                values[destination] = int(raw_value)
            elif key == "task-id":
                values["task_id"] = canonical_uuid(raw_value)
            elif key == "slug":
                values["slug"] = validate_task_slug(raw_value)
            elif key == "expected-default":
                if re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", raw_value) is None:
                    raise ValueError(
                        "--expected-default requires one full Git object ID"
                    )
                values["expected_default"] = raw_value
            else:
                values[destination] = raw_value
            index += 2
            continue
        raise ValueError(f"unknown task start argument: {argument}")
    if values["pr"] is not None and values["issue"] is not None:
        raise ValueError("--pr and --issue are mutually exclusive")
    if values["pr"] is not None and "base" in seen:
        raise ValueError("--pr and --base are mutually exclusive")
    if values["expected_default"] is not None and (
        "base" in seen or values["pr"] is not None
    ):
        raise ValueError(
            "--expected-default is valid only when --base and --pr are omitted"
        )
    if trailing is not None:
        if values["goal"] is not None:
            raise ValueError("--goal and goal arguments after -- are mutually exclusive")
        if not trailing:
            raise ValueError("task start requires a goal after --")
        values["goal_args"] = tuple(trailing)
    elif values["goal"] is not None:
        values["goal_args"] = (values["goal"],)
    else:
        raise ValueError("task start requires --goal TEXT or goal arguments after --")
    for argument in values["goal_args"]:
        if argument == ";" or argument.endswith(";"):
            raise ValueError(
                "goal arguments must not end with ';' (tmux treats a trailing "
                "semicolon as a command separator); rephrase the goal or pass it "
                "as one --goal string that does not end in ';'"
            )
    label = " ".join(values["goal_args"])
    if not 1 <= len(label) <= 200:
        raise ValueError("task goal must contain 1-200 characters")
    values["label"] = label
    values["base_explicit"] = "base" in seen
    if values["tui_worker"] and not {"json", "detach", "task-id"}.issubset(seen):
        raise ValueError(
            "--tui-worker requires explicit --json, --detach, and --task-id"
        )
    if values["json"]:
        values["detach"] = True
    return values


def _attach_tokens(adapter: TmuxAdapter, session: str) -> list[str]:
    socket = [] if adapter.socket is None else ["-L", adapter.socket]
    return [adapter.executable, *socket, "attach-session", "-t", session]


def _run_popup(
    adapter: TmuxAdapter,
    config,
    session: str,
    slug: str,
    env: Mapping[str, str],
) -> str | None:
    pane = env.get("TMUX_PANE")
    client = None if not pane else adapter.caller_client(pane)
    if client is None:
        attach = shlex.join(_attach_tokens(adapter, session))
        return (
            "asha control: no tmux client is attached to this session; "
            f"attach with: {attach}"
        )
    argv = adapter.popup_argv(
        client=client, session=session,
        width=config.popup_width, height=config.popup_height,
    )
    try:
        result = subprocess.run(argv, shell=False, check=False)
    except OSError as exc:
        raise LaunchError(f"tmux popup could not be invoked: {exc}") from exc
    if result.returncode != 0:
        attach = shlex.join(_attach_tokens(adapter, session))
        return (
            f"asha control: popup attach failed with status {result.returncode}; "
            f"task {slug} is still running; attach with: {attach}"
        )
    return None


def _guard_colocated_sync(jj: JjAdapter, repository: RepositoryFacts) -> None:
    working_copy_parent = jj.working_copy_parent(repository.root)
    git_head = jj.git_head_exact(repository.root)
    remediation = colocated_sync_remediation(
        repository.root, git_head, working_copy_parent,
    )
    if remediation is not None:
        raise ValueError(remediation)


def _path_entry_exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


_VERIFIED_COLOCATION_REAUTH_KINDS = {
    "verified_root_hardening_candidate",
    "verified_device_rebind_candidate",
}


def _ensure_colocated(
    jj: JjAdapter, selection: RepositorySelection,
    intents: ColocationIntentStore | None = None,
    *, reauth_operation: str | None = None,
    reauth_semantic: Any | None = None,
    pre_enable_plan: PlainGitPreEnablePlan | None = None,
) -> tuple[dict[str, str], ...]:
    """Recheck and, for plain Git, initialize under the source-start lock."""
    if pre_enable_plan is not None:
        revalidate_plain_git_pre_enable_plan(pre_enable_plan, jj=jj)
    try:
        assessment = None if intents is None else intents.classify(selection.root)
    except JjError as exc:
        intent_path = intents.path(selection.root)
        raise ValueError(
            "Control colocation intent is stale or binding-mismatched; task "
            f"start will refuse {selection.root}. Inspect `jj status`, Git "
            f"status/refs, and {intent_path} before repair: {exc}"
        ) from exc
    if assessment is not None and assessment.kind == "mismatch":
        intent_path = intents.path(selection.root)
        raise ValueError(
            "Control colocation intent is stale or binding-mismatched; task "
            f"start will refuse {selection.root}. Inspect `jj status`, Git "
            f"status/refs, and {intent_path}; the retained record was not "
            f"rewritten: {assessment.detail or 'repository facts differ'}"
        )
    if (
        assessment is not None
        and assessment.kind in _VERIFIED_COLOCATION_REAUTH_KINDS
    ):
        device_rebind = assessment.kind == "verified_device_rebind_candidate"
        try:
            operation_before = (
                reauth_operation
                if reauth_operation is not None else jj.pin_operation(selection.root)
            )
            semantic_before = (
                reauth_semantic
                if reauth_semantic is not None else jj._git_semantic_state(
                    selection.root, include_jj_refs=True,
                )
            )
            repository = jj.preflight(selection.root)
            git_binding = inspect_git_marker(selection.root)
            if (
                git_binding is None or repository.root != selection.root
                or repository.git_root != git_binding.target
            ):
                raise JjError(
                    "jj repository root or Git backend differs from the authenticated root"
                )
            working_copy_parent = jj.working_copy_parent(selection.root)
            git_head = jj.git_head_exact(selection.root)
            remediation = colocated_sync_remediation(
                selection.root, git_head, working_copy_parent,
            )
            if remediation is not None:
                raise JjError(remediation)
            semantic_after = jj._git_semantic_state(
                selection.root, include_jj_refs=True,
            )
            operation_after = jj.pin_operation(selection.root)
            if semantic_after != semantic_before or operation_after != operation_before:
                raise JjError(
                    "repository semantics or jj operation changed during reauthentication"
                )
            if device_rebind:
                intents.reauthenticate_device_rebind(selection.root, assessment)
            else:
                intents.reauthenticate_root_hardening(selection.root, assessment)
        except JjError as exc:
            intent_path = intents.path(selection.root)
            cause = (
                "verified colocation filesystem device renumbering"
                if device_rebind else "verified colocation root hardening"
            )
            raise ValueError(
                f"{cause} could not be reauthenticated; "
                f"the retained intent at {intent_path} was not rewritten. Inspect "
                f"`jj status` and Git status/refs before retrying: {exc}"
            ) from exc
        assessment = intents.classify(selection.root)
        if assessment.kind != "verified":
            raise ValueError(
                "verified colocation reauthentication did not produce an exact "
                "binding; the repository remains unavailable to task start"
            )
        print(
            (
                "Control reauthenticated verified jj colocation after coherent "
                "filesystem device renumbering"
                if device_rebind else
                "Control reauthenticated verified jj colocation after safe "
                "repository root permission hardening"
            ),
            file=sys.stderr, flush=True,
        )
    intent = (
        None if assessment is None or assessment.kind == "missing"
        else assessment.value
    )
    if intent is not None and intent["state"] == "intent":
        intent_path = intents.path(selection.root)
        raise ValueError(
            "ambiguous Control colocation intent was retained; refusing to "
            f"adopt or overwrite {selection.root}/.jj. Run `jj status` in "
            f"{selection.root}, inspect Git status/refs and {intent_path}, then "
            "complete or repair colocation. Only after verification, remove "
            f"the intent with `rm -- {shlex.quote(str(intent_path))}` and retry"
        )
    if intent is not None and intent["state"] == "verified":
        jj.preflight(selection.root)
        return ()
    if not selection.plain_git:
        jj.preflight(selection.root)
        return ()
    marker = selection.root / ".jj"
    if _path_entry_exists(marker):
        try:
            jj.preflight(selection.root)
        except JjError as exc:
            raise ValueError(
                "existing .jj metadata is malformed or incomplete; refusing "
                f"to overwrite it: {exc}. Run `jj status` in {selection.root} "
                "and repair or remove it only after verifying ownership"
            ) from exc
        # A concurrent start initialized and verified the repository while this
        # process waited for the deterministic source lock.
        return ()
    if intents is not None:
        if pre_enable_plan is not None:
            revalidate_plain_git_pre_enable_plan(pre_enable_plan, jj=jj)
        intents.begin(selection.root)
    try:
        if pre_enable_plan is not None:
            revalidate_plain_git_pre_enable_plan(pre_enable_plan, jj=jj)
        mutation = (
            jj.init_colocated(selection.root)
            if pre_enable_plan is None else
            jj.init_colocated(
                selection.root, expected_binding=pre_enable_plan.source_binding,
            )
        )
        if pre_enable_plan is not None:
            revalidate_plain_git_pre_enable_plan(pre_enable_plan, jj=jj)
    except KeyboardInterrupt:
        intent_path = None if intents is None else intents.path(selection.root)
        intent_detail = "" if intent_path is None else f"; intent: {intent_path}"
        print(
            "Control colocation was interrupted before authenticated "
            "verification; repository changes and ambiguous intent were "
            f"retained{intent_detail}. Run `jj status` in {selection.root}, "
            "inspect Git status/refs, and do not retry until repaired",
            file=sys.stderr, flush=True,
        )
        raise
    except Exception as exc:
        intent_path = None if intents is None else intents.path(selection.root)
        intent_detail = "" if intent_path is None else f"; intent: {intent_path}"
        raise ValueError(
            "Control colocation did not reach authenticated verification; "
            f"repository changes and intent were retained{intent_detail}. Run "
            f"`jj status` in {selection.root}, inspect Git status/refs, and "
            f"repair before retrying: {exc}"
        ) from exc
    try:
        print(
            f"Source mutation: {mutation['detail']}", file=sys.stderr, flush=True,
        )
        jj.preflight(selection.root)
        if intents is not None:
            intents.mark_verified(selection.root)
    except KeyboardInterrupt:
        intent_path = None if intents is None else intents.path(selection.root)
        intent_detail = "" if intent_path is None else f"; intent: {intent_path}"
        print(
            "Control colocation verification was interrupted; repository "
            f"enablement and ambiguous intent were retained{intent_detail}. "
            f"Run `jj status` in {selection.root}, inspect Git status/refs, "
            "and do not retry until repaired",
            file=sys.stderr, flush=True,
        )
        raise
    except Exception as exc:
        intent_path = None if intents is None else intents.path(selection.root)
        intent_detail = "" if intent_path is None else f" and {intent_path}"
        raise ValueError(
            "jj colocation completed but Control could not authenticate its "
            f"verified state; repository enablement and intent were retained at "
            f"{selection.root}{intent_detail}. Run `jj status` and inspect Git "
            f"status/refs before retrying: {exc}"
        ) from exc
    return (mutation,)


def _requested_source(parsed: Mapping[str, Any]) -> tuple[str, int | None]:
    if parsed["pr"] is not None:
        return "pr", parsed["pr"]
    if parsed["issue"] is not None:
        return "issue", parsed["issue"]
    return "ad-hoc", None


def _requested_base(parsed: Mapping[str, Any]) -> str:
    if parsed["pr"] is not None:
        return f"PR #{parsed['pr']} head"
    return parsed["base"]


def _existing_task_difference(
    task: Mapping[str, Any], *, source: Path, parsed: Mapping[str, Any],
    harness: str, role: str,
) -> str | None:
    source_kind, source_number = _requested_source(parsed)
    comparisons = (
        ("repository root", task["repository"]["root"], str(source)),
        ("source kind", task["source"]["kind"], source_kind),
        ("source number", task["source"]["number"], source_number),
        ("requested base", task["jj"]["requested_base"], _requested_base(parsed)),
    )
    for field, stored, requested in comparisons:
        if stored != requested:
            return field
    if parsed["slug"] is not None and task["slug"] != parsed["slug"]:
        return "slug"
    if not task["runs"]:
        return "primary run"
    primary = task["runs"][0]
    for field, stored, requested in (
        ("primary run harness", primary["harness"], harness),
        ("primary run role", primary["role"], role),
        ("label", task["label"], parsed["label"]),
    ):
        if stored != requested:
            return field
    return None


def _stored_start_result(task: dict[str, Any]) -> dict[str, Any]:
    primary = task["runs"][0]
    return {
        "task": task,
        "run": primary,
        "session": task["tmux"]["session"],
        "pane": primary["pane_id"],
        "workspace": {
            "path": task["jj"]["workspace_path"],
            "name": task["jj"]["workspace_name"],
            "change_id": task["jj"]["change_id"],
        },
    }


def _progress(message: str) -> None:
    """Human progress on stderr, only when a person is watching a terminal.

    Scripted callers (JSON, orchestration dispatch, tests) capture stderr and
    must not see progress prose; the TUI suspends curses around task start,
    so this is what the operator sees while a large checkout runs.
    """
    try:
        if sys.stderr.isatty():
            print(message, file=sys.stderr, flush=True)
    except (OSError, ValueError):
        pass


def _emit_start_result(
    parsed: Mapping[str, Any], env: Mapping[str, str], config,
    result: dict[str, Any], adapter: TmuxAdapter, *,
    source_mutations: list[dict[str, str]], existing: bool,
) -> int:
    launched, run = result["task"], result["run"]
    attach = shlex.join(_attach_tokens(adapter, launched["tmux"]["session"]))
    payload = {
        "contract": "asha.control-task-start.v1",
        **result,
        "source_mutations": source_mutations,
        "attach": attach,
        "existing": existing,
    }
    if parsed["json"]:
        _json(payload)
        return 0
    if existing:
        print("Existing task (unchanged):")
    print(f"Task: {launched['slug']}")
    print(f"Task ID: {launched['task_id']}")
    print(f"Workspace: {launched['jj']['workspace_path']}")
    print(f"jj name: {launched['jj']['workspace_name']}")
    print(f"Change: {launched['jj']['change_id']}")
    print(f"Base commit: {launched['jj']['base_commit_id']}")
    print(
        f"Tmux: {launched['tmux']['session']}:{launched['tmux']['window']} "
        f"{run['pane_id']}"
    )
    print(f"Run: {run['run_id']}")
    if existing:
        if not env.get("TMUX") and not parsed["detach"]:
            print("Attach: " + attach)
        return 0
    if env.get("TMUX") and not parsed["detach"]:
        refusal = _run_popup(
            adapter, config=config, session=launched["tmux"]["session"],
            slug=launched["slug"], env=env,
        )
        if refusal is not None:
            print(refusal, file=sys.stderr)
    elif not parsed["detach"]:
        print("Attach: " + attach)
    return 0


def _start_new_task(
    parsed: Mapping[str, Any], env: Mapping[str, str], config, jj: JjAdapter,
    source: Path, *, task_id: str | None, selected_harness: str,
    selected_role: str,
    initial_source_mutations: Sequence[dict[str, str]] = (),
    preflight_request: PrepareRequest | None = None,
    pre_enable_plan: PlainGitPreEnablePlan | None = None,
) -> int:
    repository = jj.preflight(source)
    if pre_enable_plan is None:
        # Compatibility for direct/internal callers. Normal starts already
        # crossed this guard immediately before universal immutable preflight.
        _guard_colocated_sync(jj, repository)
    task_source = (
        {"kind": "ad-hoc", "number": None, "url": None}
        if preflight_request is None else dict(preflight_request.source)
    )
    requested_base = (
        parsed["base"] if preflight_request is None
        else preflight_request.requested_base
    )
    resolved_base_commit_id = (
        None if preflight_request is None
        else preflight_request.resolved_base_commit_id
    )
    source_mutations: list[dict[str, str]] = [
        dict(mutation) for mutation in initial_source_mutations
    ]
    slug_input = parsed["label"]
    github_number = parsed["pr"] if parsed["pr"] is not None else parsed["issue"]
    if github_number is not None:
        kind = "pr" if parsed["pr"] is not None else "issue"
        github = GithubAdapter()
        if preflight_request is None:
            raise ValueError(
                "GitHub task start reached mutation without retained preflight metadata"
            )
        title = preflight_request.github_title
        remote = preflight_request.pr_remote
        print(
            f"GitHub {kind.upper()} #{github_number}: {title}",
            file=sys.stderr,
        )
        slug_input = f"{source.name}-{kind}-{github_number}"
        if kind == "pr":
            if remote is None:
                raise ValueError("preflight did not retain the selected pull-request remote")
            for mutation in github.fetch_pr_head(
                source, remote, github_number, git=jj,
            ):
                source_mutations.append(mutation)
                print(f"Source mutation: {mutation['detail']}", file=sys.stderr)
            controller_ref = (
                f"refs/remotes/{remote.name}/asha-control-pr-{github_number}"
            )
            fetched_head = jj.resolve_git_commit(source, controller_ref)
            if fetched_head != resolved_base_commit_id:
                raise ValueError(
                    "pull-request head changed after metadata inspection; the "
                    "controller ref was retained for inspection, but no jj import, "
                    "task, or workspace mutation was attempted"
                )
            if pre_enable_plan is None:
                raise ValueError("pull-request start lacks retained prerequisite proof")
            revalidate_pr_source_proof_after_fetch(pre_enable_plan, jj=jj)
            requested_base = f"PR #{github_number} head"
    _guard_colocated_sync(jj, repository)
    default_resolution = None
    if not parsed["base_explicit"] and parsed["pr"] is None:
        default_resolution = jj.resolve_default_base(source)
        if (
            parsed.get("expected_default") is not None
            and parsed["expected_default"] != default_resolution.commit_id
        ):
            raise ValueError(
                "default base changed after the TUI preview; review the new "
                "default or select/type an explicit --base"
            )
        if (
            pre_enable_plan is not None
            and pre_enable_plan.default_base_resolution is not None
            and default_resolution != pre_enable_plan.default_base_resolution
        ):
            raise ValueError(
                "default base changed after preflight; review the new default "
                "or pass an explicit --base"
            )
        if (
            resolved_base_commit_id is not None
            and resolved_base_commit_id != default_resolution.commit_id
        ):
            raise ValueError(
                "default base changed after preflight; review the new default "
                "or pass an explicit --base"
            )
        resolved_base_commit_id = default_resolution.commit_id
    for mutation in jj.import_git(source):
        source_mutations.append(mutation)
        print(f"Source mutation: {mutation['detail']}", file=sys.stderr)
    task_id = task_id or new_uuid()
    request = PrepareRequest(
        repository=source,
        requested_base=requested_base,
        task_id=task_id,
        slug=(
            preflight_request.slug if preflight_request is not None
            else parsed["slug"] or _slug(slug_input)
        ),
        label=parsed["label"],
        source=task_source,
        resolved_base_commit_id=resolved_base_commit_id,
        expected_default_commit_id=parsed.get("expected_default"),
    )
    base_progress = requested_base
    if default_resolution is not None:
        base_progress = (
            f"{', '.join(default_resolution.references)} "
            f"@ {default_resolution.commit_id}"
        )
    _progress(
        f"Preparing jj workspace for {source} at {base_progress} "
        "(checking out the base tree; a large repository takes a while)..."
    )
    prepared = prepare_task_workspace(config, request, jj=jj)
    socket = prepared["tmux"]["socket"]
    adapter = TmuxAdapter(socket=None if socket == "default" else socket)
    _progress(f"Workspace ready at {prepared['jj']['workspace_path']}; launching {selected_harness}...")
    result = launch_task(
        config, prepared, tmux=adapter, harness=selected_harness,
        goal_args=parsed["goal_args"], role=selected_role,
    )
    return _emit_start_result(
        parsed, env, config, result, adapter,
        source_mutations=source_mutations, existing=False,
    )


def _build_start_preflight_request(
    parsed: Mapping[str, Any], config, jj: JjAdapter, source: Path,
    task_id: str,
) -> PrepareRequest:
    """Read GitHub metadata and exact remote selection before source mutation."""
    task_source = {"kind": "ad-hoc", "number": None, "url": None}
    requested_base = parsed["base"]
    resolved_base_commit_id = None
    github_title = None
    pr_remote = None
    slug_input = parsed["label"]
    github_number = parsed["pr"] if parsed["pr"] is not None else parsed["issue"]
    if github_number is not None:
        github = GithubAdapter()
        github.preflight()
        kind = "pr" if parsed["pr"] is not None else "issue"
        metadata = (
            github.pr_metadata(source, github_number)
            if kind == "pr" else github.issue_metadata(source, github_number)
        )
        task_source = {
            "kind": kind, "number": github_number, "url": metadata["url"],
        }
        github_title = metadata["title"]
        slug_input = f"{source.name}-{kind}-{github_number}"
        if kind == "pr":
            pr_remote = github.pr_remote(
                source, metadata["url"], github_number, git=jj,
            )
            resolved_base_commit_id = metadata["headRefOid"]
            # The pre-mutation proof and repair offer bind the exact immutable
            # object ID. The durable task record restores the human PR label
            # only after the selected head is fetched and re-proved in source.
            requested_base = resolved_base_commit_id
    return PrepareRequest(
        repository=source, requested_base=requested_base, task_id=task_id,
        slug=parsed["slug"] or _slug(slug_input), label=parsed["label"],
        source=task_source, resolved_base_commit_id=resolved_base_commit_id,
        github_title=github_title, pr_remote=pr_remote,
        expected_default_commit_id=parsed.get("expected_default"),
    )


def _preflight_plain_git_start(
    parsed: Mapping[str, Any], config, jj: JjAdapter, source: Path,
    task_id: str, *, existing_jj: bool = False,
    request: PrepareRequest | None = None,
) -> tuple[PrepareRequest, PlainGitPreEnablePlan]:
    """Build and validate the plain-Git request before durable enablement."""
    request = request or _build_start_preflight_request(
        parsed, config, jj, source, task_id,
    )
    plan = preflight_plain_git_enablement(
        config, request, jj=jj,
        base_explicit=(
            parsed["base_explicit"] or request.source.get("kind") == "pr"
        ),
        existing_jj=existing_jj,
    )
    if (
        plan.resolved_base_commit_id == request.resolved_base_commit_id
    ):
        return request, plan
    return PrepareRequest(
        repository=request.repository, requested_base=request.requested_base,
        task_id=request.task_id, slug=request.slug, label=request.label,
        source=request.source,
        resolved_base_commit_id=plan.resolved_base_commit_id,
        github_title=request.github_title, pr_remote=request.pr_remote,
        expected_default_commit_id=request.expected_default_commit_id,
    ), plan


def _start_command_inner(args: list[str], env: Mapping[str, str]) -> int:
    parsed = _parse_start(args)
    config = load_config(env)
    selected_harness = harness_api.validate_harness(
        parsed["harness"] or config.default_harness
    )
    selected_role = harness_api.validate_role(parsed["role"])
    if shutil.which(selected_harness) is None:
        raise ValueError(
            f"harness {selected_harness!r} is not installed or not on PATH; "
            "install it or pass --harness <claude|codex|copilot|opencode>"
        )
    raw_root = env.get("ASHA_ROOT")
    if raw_root is None:
        asha_root = Path(__file__).resolve().parents[2]
    else:
        supplied = Path(raw_root)
        if not supplied.is_absolute():
            raise ValueError("ASHA_ROOT must be an absolute path")
        asha_root = supplied.resolve()
    harness_api.launch_argv(asha_root, selected_harness, parsed["goal_args"])
    jj = JjAdapter()
    selected = _repo_argument(parsed["repo"], config, jj)
    # Older injected unit adapters returned a Path at this seam. Treat that as
    # an already-jj selection; the production selector always returns the
    # explicit read-only classification above.
    selection = (
        selected if isinstance(selected, RepositorySelection)
        else RepositorySelection(Path(selected), plain_git=False)
    )
    source = selection.root
    supplied_task_id = parsed["task_id"] is not None
    task_id = parsed["task_id"] or new_uuid()

    tasks = TaskStore(config)
    journals = CreationJournalStore(config)
    colocation_intents = ColocationIntentStore(config)
    with tasks.transaction_lock(task_id):
        try:
            existing = tasks.read(task_id)
        except StoreError as exc:
            if str(exc) != f"task not found: {task_id}":
                raise
            existing = None
        if existing is not None:
            if not supplied_task_id:
                raise ValueError("generated task ID collided with an existing task; retry")
            if existing["lifecycle"] == "creating":
                raise ValueError(
                    f"task {task_id} has an interrupted creation; run "
                    f"`asha task recover {task_id}` then retry"
                )
            difference = _existing_task_difference(
                existing, source=source, parsed=parsed,
                harness=selected_harness, role=selected_role,
            )
            if difference is not None:
                raise ValueError(
                    f"task {task_id} is already registered with different "
                    f"parameters: {difference}"
                )
            socket = existing["tmux"]["socket"]
            adapter = TmuxAdapter(socket=None if socket == "default" else socket)
            return _emit_start_result(
                parsed, env, config, _stored_start_result(existing), adapter,
                source_mutations=[], existing=True,
            )
        try:
            journals.read(task_id)
        except JournalError as exc:
            if str(exc) != f"creation journal not found: {task_id}":
                raise
        else:
            raise ValueError(
                f"task {task_id} has an interrupted creation; run "
                f"`asha task recover {task_id}` then retry"
            )
        with colocation_intents.mutation_lock(source):
            preflight_request = None
            pre_enable_plan = None
            assessment = colocation_intents.classify(source)
            if not selection.plain_git:
                # Repository coherence is itself read-only and precedes base
                # resolution, so a pending/unborn Git HEAD gets its exact
                # remediation instead of an incidental revset failure.
                _guard_colocated_sync(jj, jj.preflight(source))
            reauth_operation = None
            reauth_semantic = None
            if assessment.kind in _VERIFIED_COLOCATION_REAUTH_KINDS:
                device_rebind = assessment.kind == "verified_device_rebind_candidate"
                try:
                    reauth_operation = jj.pin_operation(source)
                    reauth_semantic = jj._git_semantic_state(
                        source, include_jj_refs=True,
                    )
                except JjError as exc:
                    cause = (
                        "verified colocation device renumbering"
                        if device_rebind else "verified colocation hardening"
                    )
                    raise ValueError(
                        f"{cause} could not begin stable "
                        "reauthentication; the retained intent was not rewritten. "
                        f"Inspect `jj status` and Git status/refs before retrying: {exc}"
                    ) from exc
            if parsed["pr"] is not None or parsed["issue"] is not None:
                preflight_request = _build_start_preflight_request(
                    parsed, config, jj, source, task_id,
                )
            try:
                # Every Git-backed start crosses the immutable context gate
                # before colocation, import, task state, or workspace mutation.
                preflight_request, pre_enable_plan = _preflight_plain_git_start(
                    parsed, config, jj, source, task_id,
                    existing_jj=not selection.plain_git,
                    request=preflight_request,
                )
                revalidate_plain_git_pre_enable_plan(pre_enable_plan, jj=jj)
            except PreparationPrerequisiteError as exc:
                offer = capture_prerequisite_offer(config, exc)
                raise StartPrerequisiteRefusal(
                    offer, task_id=task_id, tui_worker=parsed["tui_worker"],
                ) from exc
            initial_mutations = _ensure_colocated(
                jj, selection, colocation_intents,
                reauth_operation=reauth_operation,
                reauth_semantic=reauth_semantic,
                pre_enable_plan=pre_enable_plan,
            )
            return _start_new_task(
                parsed, env, config, jj, source, task_id=task_id,
                selected_harness=selected_harness, selected_role=selected_role,
                initial_source_mutations=initial_mutations,
                preflight_request=preflight_request,
                pre_enable_plan=pre_enable_plan,
            )


def _start_command(
    args: list[str], env: Mapping[str, str], *,
    preserve_sigterm_handler: bool = False,
) -> int:
    if preserve_sigterm_handler:
        return _start_command_inner(args, env)
    previous = signal.getsignal(signal.SIGTERM)

    def interrupt_on_term(signum, frame) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupt_on_term)
    try:
        return _start_command_inner(args, env)
    finally:
        signal.signal(signal.SIGTERM, previous)


def _attach_command(args: list[str], env: Mapping[str, str]) -> int:
    selector: str | None = None
    run_id: str | None = None
    index = 0
    while index < len(args):
        if args[index] == "--run":
            if run_id is not None or index + 1 >= len(args):
                raise ValueError("task attach --run requires exactly one run ID")
            run_id = args[index + 1]
            index += 2
        elif args[index].startswith("-") or selector is not None:
            raise ValueError("task attach requires one task selector and optional --run RUN_ID")
        else:
            selector = args[index]
            index += 1
    if selector is None:
        raise ValueError("task attach requires one task ID or exact slug")
    config = load_config(env)
    task = TaskStore(config).resolve(selector)
    socket = task["tmux"]["socket"]
    adapter = TmuxAdapter(socket=None if socket == "default" else socket)
    target = view.attach_target(task, run_id, adapter=adapter)
    adapter.select_target(target.session, target.window, target.pane_id)
    if env.get("TMUX"):
        refusal = _run_popup(
            adapter, config, task["tmux"]["session"], task["slug"], env,
        )
        if refusal is not None:
            print(refusal, file=sys.stderr)
            return 2
    else:
        print(shlex.join(_attach_tokens(adapter, task["tmux"]["session"])))
    return 0


def _stop_command(args: list[str], env: Mapping[str, str]) -> int:
    terminate = False
    selectors: list[str] = []
    for argument in args:
        if argument == "--terminate":
            if terminate:
                raise ValueError("--terminate may be specified only once")
            terminate = True
        elif argument.startswith("-"):
            raise ValueError(f"unknown task stop argument: {argument}")
        else:
            selectors.append(argument)
    if len(selectors) != 1:
        raise ValueError("task stop requires exactly one task ID or exact slug")
    config = load_config(env)
    store = TaskStore(config)
    task = store.resolve(selectors[0])
    result = stop_task(config, task, tasks=store, terminate=terminate)
    print(f"Sent SIG{result['signal']} to run {result['run_id']} (PID {result['pid']}).")
    return 0


def _archive_command(args: list[str], env: Mapping[str, str]) -> int:
    if len(args) != 1 or args[0].startswith("-"):
        raise ValueError("task archive requires exactly one task ID or exact slug")
    config = load_config(env)
    store = TaskStore(config)
    journals = CreationJournalStore(config)
    jj = JjAdapter()
    task = store.resolve(args[0])
    socket = task["tmux"]["socket"]
    presentation = TmuxAdapter(socket=None if socket == "default" else socket)
    archived = archive_task(
        config, task, tasks=store,
        adapters=LiveAdapters(config=config, tmux=presentation, jj=jj),
        journals=journals, jj=jj, presentation=presentation,
    )
    print(f"Archived task {archived['slug']} ({archived['task_id']}).")
    return 0


def _unarchive_command(args: list[str], env: Mapping[str, str]) -> int:
    if len(args) != 1 or args[0].startswith("-"):
        raise ValueError("task unarchive requires exactly one task ID or exact slug")
    config = load_config(env)
    store = TaskStore(config)
    task = store.resolve(args[0])
    ended = unarchive_task(config, task, tasks=store)
    print(f"Unarchived task {ended['slug']} ({ended['task_id']}).")
    return 0


def _parse_prune(args: list[str]) -> dict[str, Any]:
    values: dict[str, Any] = {
        "selectors": [], "all": False, "keep_workspace": False,
        "dry_run": False, "yes": False, "json": False,
    }
    flags = {
        "--all": "all", "--keep-workspace": "keep_workspace",
        "--dry-run": "dry_run", "--yes": "yes", "--json": "json",
    }
    for argument in args:
        if argument in flags:
            if values[flags[argument]]:
                raise ValueError(f"{argument} may be specified only once")
            values[flags[argument]] = True
        elif argument.startswith("-"):
            raise ValueError(f"unknown task prune argument: {argument}")
        else:
            values["selectors"].append(argument)
    if values["all"] == bool(values["selectors"]):
        raise ValueError("task prune requires task selectors or --all, not both")
    return values


def _prune_command(args: list[str], env: Mapping[str, str]) -> int:
    values = _parse_prune(args)
    config = load_config(env)
    store = TaskStore(config)
    journals = CreationJournalStore(config)
    jj = JjAdapter()
    if values["all"]:
        selected = [record for record in store.list() if record["lifecycle"] == "archived"]
        for skipped in store.skipped:
            print(
                f"asha task prune: skipped {skipped['name']}: {skipped['reason']}",
                file=sys.stderr,
            )
    else:
        selected = [store.resolve(selector) for selector in values["selectors"]]
    remove_workspace = not values["keep_workspace"]
    if remove_workspace and selected and not values["dry_run"] and not values["yes"]:
        if not sys.stdin.isatty() or values["json"]:
            raise ValueError(
                "task prune removes workspace directories; pass --yes, "
                "--keep-workspace, or --dry-run when stdin is not a terminal "
                "or --json is requested"
            )
        print(
            f"Prune {len(selected)} archived task(s): kill their dead tmux "
            "sessions, forget their jj workspaces, and remove the workspace "
            "directories (jj changes remain in the source repository)."
        )
        answer = input("Proceed? [y/N] ").strip().casefold()
        if answer not in {"y", "yes"}:
            print("Prune cancelled.")
            return 2
    context = assemble_prune_context(
        config, tasks=store, env=dict(env), remove_workspace=remove_workspace,
    )
    results = []
    for task in selected:
        socket = task["tmux"]["socket"]
        adapter = TmuxAdapter(socket=None if socket == "default" else socket)
        results.append(prune_one_task(
            config, task, tasks=store, journals=journals, jj=jj,
            remove_workspace=remove_workspace, dry_run=values["dry_run"],
            context=context, tmux=adapter,
        ).as_dict())
    outcomes = {result["outcome"] for result in results}
    exit_code = 2 if outcomes & {"refused", "partial"} else 0
    if values["json"]:
        payload = {
            "contract": PRUNE_CONTRACT, "dry_run": values["dry_run"],
            "results": results,
        }
        if context.bindings_error is not None:
            payload["orchestration_bindings_error"] = context.bindings_error
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return exit_code
    for result in results:
        print(
            f"{result['outcome']:<17} {result['slug']} ({result['task_id']}): "
            f"session {result['session']['action']} ({result['session']['detail']}); "
            f"workspace {result['workspace']['action']} ({result['workspace']['detail']})"
        )
    if not results:
        print("No archived tasks to prune.")
    return exit_code


def _recover_command(args: list[str], env: Mapping[str, str]) -> int:
    selector: str | None = None
    adopt = False
    confirmed = False
    values: dict[str, str | None] = {"harness": None, "role": None, "goal": None}
    seen: set[str] = set()
    index = 0
    while index < len(args):
        argument = args[index]
        if argument in {"--adopt", "--yes"}:
            if argument in seen:
                raise ValueError(f"{argument} may be specified only once")
            seen.add(argument)
            adopt = adopt or argument == "--adopt"
            confirmed = confirmed or argument == "--yes"
            index += 1
            continue
        if argument in {"--harness", "--role", "--goal"}:
            if argument in seen or index + 1 >= len(args):
                raise ValueError(f"{argument} requires exactly one value")
            seen.add(argument)
            values[argument[2:]] = args[index + 1]
            index += 2
            continue
        if argument.startswith("-") or selector is not None:
            raise ValueError("task recover requires one task ID or exact slug and valid recovery options")
        selector = argument
        index += 1
    if selector is None:
        raise ValueError("task recover requires exactly one task ID or exact slug")
    adoption_options = bool(seen & {"--adopt", "--yes", "--harness", "--role", "--goal"})
    if adoption_options and not (
        adopt and confirmed and all(values.values())
        and seen == {"--adopt", "--yes", "--harness", "--role", "--goal"}
    ):
        raise ValueError(
            "retained-state adoption requires all of --adopt --yes --harness H "
            "--role ROLE --goal TEXT"
        )
    config = load_config(env)
    store = TaskStore(config)
    journals = CreationJournalStore(config)
    task = store.resolve(selector)
    selected_harness = None
    selected_role = None
    goal = None
    if adopt:
        assert values["harness"] is not None
        assert values["role"] is not None
        assert values["goal"] is not None
        selected_harness = harness_api.validate_harness(values["harness"])
        selected_role = harness_api.validate_role(values["role"])
        goal = values["goal"]
        if goal != task["label"]:
            raise ValueError("adoption --goal must exactly match the durable task label")
        if shutil.which(selected_harness) is None:
            raise ValueError(
                f"harness {selected_harness!r} is not installed or not on PATH"
            )
        raw_root = env.get("ASHA_ROOT")
        asha_root = (
            Path(__file__).resolve().parents[2]
            if raw_root is None else Path(raw_root)
        )
        if not asha_root.is_absolute():
            raise ValueError("ASHA_ROOT must be an absolute path")
        harness_api.launch_argv(asha_root, selected_harness, (goal,))
    socket = task["tmux"]["socket"]
    adapter = TmuxAdapter(socket=None if socket == "default" else socket)
    result = recover_task(
        config, task, tasks=store, journals=journals,
        tmux=adapter, jj=JjAdapter(),
        adopt=adopt, harness=selected_harness, role=selected_role, goal=goal,
    )
    print(result["message"])
    if result["recovery_commands"] is not None:
        print(result["recovery_commands"])
    print(f"Lifecycle: {result['task']['lifecycle']}")
    print(f"Journal phase: {result['journal']['phase']}")
    return 0


def _task_command(args: list[str], env: Mapping[str, str]) -> int:
    if not args or args[0] in {"-h", "--help", "help"}:
        _task_usage(sys.stdout if args else sys.stderr)
        return 0 if args else 2
    command, tail = args[0], args[1:]
    if command in {"report", "result", "seal"}:
        # Lazy by contract: malformed orchestration configuration cannot alter
        # any ordinary Control task command.
        from .orchestration.cli import task_main as orchestration_task_main

        return orchestration_task_main(args, env=env)
    if command not in {
        "start", "list", "show", "attach", "stop", "archive", "unarchive",
        "recover", "prune", "reconcile", "doctor",
    }:
        print("asha task: unknown subcommand", file=sys.stderr)
        return 2
    if command == "start":
        return _start_command(tail, env)
    if command == "attach":
        return _attach_command(tail, env)
    if command == "stop":
        return _stop_command(tail, env)
    if command == "archive":
        return _archive_command(tail, env)
    if command == "unarchive":
        return _unarchive_command(tail, env)
    if command == "recover":
        return _recover_command(tail, env)
    if command == "prune":
        return _prune_command(tail, env)
    tail, json_output = _parse_json_flag(tail)
    config = load_config(env)
    store = TaskStore(config)
    journals = CreationJournalStore(config)
    jj = JjAdapter()
    observed = datetime.now(timezone.utc)

    def clock() -> datetime:
        return observed

    # list/show/reconcile must report LIVE evidence. Increments 3 and 4 built
    # and tested the live tmux, process, jj, and event adapters, but this call
    # site kept Increment 1's placeholder, so every CLI reconciliation reported
    # "not implemented in Increment 1" and fell back to the stored record.
    adapters = LiveAdapters(config=config, jj=jj, now=clock)

    if command == "list":
        if tail:
            raise ValueError("task list accepts only --json")
        tasks = []
        listed_records = store.list()
        for skipped in store.skipped:
            print(
                f"asha task list: skipped {skipped['name']}: {skipped['reason']}",
                file=sys.stderr,
            )
        for listed in listed_records:
            task, reconciliation = view.locked_reconciliation(
                store, journals, listed["task_id"], adapters, jj,
                presentation=adapters.tmux_adapter,
                publish_summary=False, presentation_now=clock,
            )
            tasks.append(view.task_summary(task, reconciliation))
        view.publish_server_summary(
            config, adapters.tmux_adapter, now=clock,
        )
        payload = {"contract": "asha.control-task-list.v1", "tasks": tasks}
        if store.skipped:
            payload["skipped"] = store.skipped
        if json_output:
            _json(payload)
        else:
            if not tasks:
                print("No Control tasks registered.")
            else:
                for task in tasks:
                    print(f"{task['status']:<12} {task['slug']:<32} {task['task_id']}")
        return 0

    if command == "show":
        if len(tail) != 1:
            raise ValueError("task show requires exactly one task ID or exact slug")
        resolved = store.resolve(tail[0])
        task, reconciliation = view.locked_reconciliation(
            store, journals, resolved["task_id"], adapters, jj,
            presentation=adapters.tmux_adapter,
            presentation_now=clock,
        )
        payload = {
            "contract": "asha.control-task-show.v1",
            "task": task,
            "reconciliation": reconciliation,
        }
        if json_output:
            _json(payload)
        else:
            print(f"Task:       {task['slug']}")
            print(f"Task ID:    {task['task_id']}")
            print(f"Lifecycle:  {task['lifecycle']}")
            print(f"Status:     {reconciliation['state']}")
            print(f"Repository: {task['repository']['root']}")
            print(f"JJ workspace: {task['jj']['workspace_name']}")
            print(f"Workspace:    {task['jj']['workspace_path']}")
            print(f"Base commit:  {task['jj']['base_commit_id']}")
            print(f"Change:       {task['jj']['change_id'] or 'not recorded'}")
            print(f"Working commit: {task['jj']['working_commit_id'] or 'not recorded'}")
            print(
                "Tmux:        "
                f"{task['tmux']['socket']} / {task['tmux']['session']}:{task['tmux']['window']}"
            )
            print(f"Blocker:     {reconciliation['blocker'] or 'none'}")
            print("Runs:")
            for run, derived in zip(task["runs"], reconciliation["runs"]):
                print(
                    f"  {run['run_id']}  {run['harness']}/{run['role']}  "
                    f"stored={run['state']} derived={derived['state']}"
                )
                print(
                    f"    Pane: {run['pane_id']}  PID: {run['pid']}  "
                    f"Process: {run['process_start_identity']}"
                )
                print(f"    Harness session: {run['harness_session_id'] or 'none'}")
                print(f"    Stored evidence: {run['evidence']} @ {run['evidence_at']}")
                print(f"    Derived blocker: {derived['blocker'] or 'none'}")
                print("    Evidence:")
                for evidence_item in derived["evidence"]:
                    print(
                        f"      {evidence_item['source']}={evidence_item['outcome']}: "
                        f"{evidence_item['detail']}"
                    )
            print("Evidence: collected per run above")
        return 0

    if command == "reconcile":
        if len(tail) > 1:
            raise ValueError("task reconcile accepts at most one task ID or exact slug")
        if tail:
            selected = [store.resolve(tail[0])]
        else:
            selected = store.list()
        pairs = [
            view.locked_reconciliation(
                store, journals, task["task_id"], adapters, jj,
                presentation=adapters.tmux_adapter,
                publish_summary=False, presentation_now=clock,
            )
            for task in selected
        ]
        view.publish_server_summary(
            config, adapters.tmux_adapter, now=clock,
        )
        tasks = [task for task, _ in pairs]
        results = [result for _, result in pairs]
        payload = {"contract": "asha.control-reconcile-list.v1", "results": results}
        if json_output:
            _json(payload)
        else:
            for task, result in zip(tasks, results):
                print(f"{result['state']:<12} {task['slug']:<32} {task['task_id']}")
        return 0

    if tail:
        raise ValueError("task doctor accepts only --json")
    payload = run_doctor(config)
    if json_output:
        _json(payload)
    else:
        for probe in payload["probes"]:
            print(f"{probe['outcome']:<11} {probe['name']}: {probe['detail']}")
    return 0 if payload["ok"] else 1


def main(argv: Sequence[str] | None = None, *, env: Mapping[str, str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    values = os.environ if env is None else env
    try:
        if not args:
            print("asha control core requires the `task` or `control` route", file=sys.stderr)
            return 2
        domain, tail = args[0], args[1:]
        if domain == "task":
            return _task_command(tail, values)
        if domain == "initiative":
            # Lazy by contract: malformed orchestration configuration must not
            # change any ordinary Control command.
            from .orchestration.cli import main as orchestration_main
            return orchestration_main(args, env=values)
        if domain == "control":
            if tail and tail[0] in {"-h", "--help", "help"}:
                _control_usage()
                return 0
            if tail == ["tmux"]:
                config = load_config(values)
                print(TmuxAdapter().integration_snippet(
                    session_prefix=config.session_prefix,
                ), end="")
                return 0
            if tail and tail[0] == "event":
                return _event_command(tail[1:], values)
            if not tail:
                from .tui import run_tui
                return run_tui(values)
            _control_usage(sys.stderr)
            return 2
        print("unknown Control route", file=sys.stderr)
        return 2
    except StartPrerequisiteRefusal as exc:
        if exc.tui_worker and exc.task_id is not None:
            print(encode_worker_refusal(exc.offer, exc.task_id).decode("utf-8"), end="")
        else:
            print(f"asha control: {exc}", file=sys.stderr)
        return 2
    except (ConfigError, StoreError, PreparationError, SourceError, LaunchError,
            TmuxError, JournalError, ValueError) as exc:
        print(f"asha control: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("asha control: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
