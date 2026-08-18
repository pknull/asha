"""Deterministic Asha Control command-line surface."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import unicodedata
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
from .jj import JjAdapter, RepositoryFacts, colocated_sync_remediation
from .launch import (
    LaunchError, archive_task, launch_task, recover_task, stop_task,
    unarchive_task,
)
from .model import canonical_uuid, new_uuid
from .prepare import PrepareRequest, PreparationError, prepare_task_workspace
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
                  [--task-id UUID]
                  [--harness H|--agent H] (--goal TEXT | -- TEXT...)
                  [--role ROLE] [--detach] [--json]
  asha task list [--json]
  asha task show <task-id|exact-slug> [--json]
  asha task attach <task-id|exact-slug> [--run RUN_ID]
  asha task stop <task-id|exact-slug> [--terminate]
  asha task archive <task-id|exact-slug>
  asha task unarchive <task-id|exact-slug>
  asha task recover <task-id|exact-slug>
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


def _repo_argument(value: str | None, config, jj: JjAdapter) -> Path:
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
    return jj.discover_root(candidate)


def _parse_start(args: list[str]) -> dict[str, Any]:
    values: dict[str, Any] = {
        "repo": None, "base": "trunk()", "harness": None, "role": "implementer",
        "goal": None, "pr": None, "issue": None, "task_id": None,
        "detach": False, "json": False,
    }
    seen: set[str] = set()
    index = 0
    trailing: list[str] | None = None
    while index < len(args):
        argument = args[index]
        if argument == "--":
            trailing = args[index + 1:]
            break
        if argument in {"--detach", "--json"}:
            key = argument[2:]
            if key in seen:
                raise ValueError(f"{argument} may be specified only once")
            seen.add(key)
            values[key] = True
            index += 1
            continue
        if argument in {"--repo", "--base", "--harness", "--agent", "--goal", "--role",
                        "--pr", "--issue", "--task-id"}:
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
            else:
                values[destination] = raw_value
            index += 2
            continue
        raise ValueError(f"unknown task start argument: {argument}")
    if values["pr"] is not None and values["issue"] is not None:
        raise ValueError("--pr and --issue are mutually exclusive")
    if values["pr"] is not None and "base" in seen:
        raise ValueError("--pr and --base are mutually exclusive")
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
        print(
            f"asha control: popup closed with status {result.returncode}; task "
            f"{slug} is still running (attach: {attach})",
            file=sys.stderr,
        )
    return None


def _guard_colocated_sync(jj: JjAdapter, repository: RepositoryFacts) -> None:
    working_copy_parent = jj.working_copy_parent(repository.root)
    git_head = jj.git_head(repository.git_root)
    remediation = colocated_sync_remediation(
        repository.root, git_head, working_copy_parent,
    )
    if remediation is not None:
        raise ValueError(remediation)


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
) -> int:
    repository = jj.preflight(source)
    _guard_colocated_sync(jj, repository)
    task_source = {"kind": "ad-hoc", "number": None, "url": None}
    requested_base = parsed["base"]
    resolved_base_commit_id = None
    source_mutations: list[dict[str, str]] = []
    slug_input = parsed["label"]
    github_number = parsed["pr"] if parsed["pr"] is not None else parsed["issue"]
    if github_number is not None:
        github = GithubAdapter()
        github.preflight()
        kind = "pr" if parsed["pr"] is not None else "issue"
        metadata = (
            github.pr_metadata(source, github_number)
            if kind == "pr"
            else github.issue_metadata(source, github_number)
        )
        print(
            f"GitHub {kind.upper()} #{github_number}: {metadata['title']}",
            file=sys.stderr,
        )
        task_source = {
            "kind": kind, "number": github_number, "url": metadata["url"],
        }
        slug_input = f"{source.name}-{kind}-{github_number}"
        if kind == "pr":
            remote = github.pr_remote(
                repository.git_root, metadata["url"], github_number,
            )
            for mutation in github.fetch_pr_head(
                repository.git_root, remote, github_number,
            ):
                source_mutations.append(mutation)
                print(f"Source mutation: {mutation['detail']}", file=sys.stderr)
            requested_base = f"PR #{github_number} head"
            resolved_base_commit_id = metadata["headRefOid"]
    _guard_colocated_sync(jj, repository)
    for mutation in jj.import_git(source):
        source_mutations.append(mutation)
        print(f"Source mutation: {mutation['detail']}", file=sys.stderr)
    task_id = task_id or new_uuid()
    request = PrepareRequest(
        repository=source,
        requested_base=requested_base,
        task_id=task_id,
        slug=_slug(slug_input),
        label=parsed["label"],
        source=task_source,
        resolved_base_commit_id=resolved_base_commit_id,
    )
    prepared = prepare_task_workspace(config, request, jj=jj)
    socket = prepared["tmux"]["socket"]
    adapter = TmuxAdapter(socket=None if socket == "default" else socket)
    result = launch_task(
        config, prepared, tmux=adapter, harness=selected_harness,
        goal_args=parsed["goal_args"], role=selected_role,
    )
    return _emit_start_result(
        parsed, env, config, result, adapter,
        source_mutations=source_mutations, existing=False,
    )


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
    source = _repo_argument(parsed["repo"], config, jj)
    task_id = parsed["task_id"]
    if task_id is None:
        return _start_new_task(
            parsed, env, config, jj, source, task_id=None,
            selected_harness=selected_harness, selected_role=selected_role,
        )

    tasks = TaskStore(config)
    journals = CreationJournalStore(config)
    with tasks.transaction_lock(task_id):
        try:
            existing = tasks.read(task_id)
        except StoreError as exc:
            if str(exc) != f"task not found: {task_id}":
                raise
            existing = None
        if existing is not None:
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
        return _start_new_task(
            parsed, env, config, jj, source, task_id=task_id,
            selected_harness=selected_harness, selected_role=selected_role,
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


def _recover_command(args: list[str], env: Mapping[str, str]) -> int:
    if len(args) != 1 or args[0].startswith("-"):
        raise ValueError("task recover requires exactly one task ID or exact slug")
    config = load_config(env)
    store = TaskStore(config)
    journals = CreationJournalStore(config)
    task = store.resolve(args[0])
    socket = task["tmux"]["socket"]
    adapter = TmuxAdapter(socket=None if socket == "default" else socket)
    result = recover_task(
        config, task, tasks=store, journals=journals,
        tmux=adapter, jj=JjAdapter(),
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
        "recover", "reconcile", "doctor",
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
    tail, json_output = _parse_json_flag(tail)
    config = load_config(env)
    store = TaskStore(config)
    journals = CreationJournalStore(config)
    jj = JjAdapter()
    # list/show/reconcile must report LIVE evidence. Increments 3 and 4 built
    # and tested the live tmux, process, jj, and event adapters, but this call
    # site kept Increment 1's placeholder, so every CLI reconciliation reported
    # "not implemented in Increment 1" and fell back to the stored record.
    adapters = LiveAdapters(config=config, jj=jj)

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
            )
            tasks.append(view.task_summary(task, reconciliation))
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
            )
            for task in selected
        ]
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
    except (ConfigError, StoreError, PreparationError, SourceError, LaunchError,
            TmuxError, JournalError, ValueError) as exc:
        print(f"asha control: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("asha control: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
