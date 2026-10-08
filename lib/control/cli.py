"""Deterministic Asha Control command-line surface."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Mapping, Sequence, Any

from .config import ConfigError, load_config
from .store import StoreError
from .tmux import TmuxAdapter, TmuxError


# Retired 2026-10-05 (Keeper, subtraction panel K1). Refused by name so the
# words never fall through to another meaning.
_TASK_RETIRED = (
    "asha task: the task substrate was retired on 2026-10-05; launch work with "
    "`asha control session launch` and read retired records with `asha initiative export`"
)
_MIGRATE_RETIRED = (
    "asha migrate: the one-shot move to the single ~/.asha root was retired on "
    "2026-10-07; no supported home still uses the pre-consolidation layout"
)
# Retired 2026-10-07 (Keeper, subtraction value call N1). `run` exits 0 so an
# installed unit (Restart=on-failure) stops instead of restarting every 5 s.
_SUPERVISOR_RETIRED = (
    "asha control supervisor: the supervisor daemon was retired on 2026-10-07. "
    "Structured session owners start where work is queued (launch, send, resume, "
    "answer), and `asha control session show` or `list` restarts a missing one. "
    "Runtime admission moved to `asha control session admission "
    "{status|pause|drain|resume|stop}`. Remove an installed unit with: "
    "systemctl --user disable --now asha-supervisor.service; "
    "rm ${XDG_CONFIG_HOME:-~/.config}/systemd/user/asha-supervisor.service; "
    "systemctl --user daemon-reload"
)


def _json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _control_usage(stream=sys.stdout) -> None:
    print("""asha control: project harness launcher and monitor

Run `asha control` in a terminal to open the Control TUI.
Use `asha control session list --json` as the non-interactive fallback.
Use `asha control projects [--match TEXT] [--json]` to list the projects sessions can launch in.
Use `asha control doctor [--json]` to check Control's dependencies, hooks and state.
Use `asha control tmux` to print the optional tmux integration snippet.
Use `asha control session launch --project PROJECT --prompt TEXT [--harness HARNESS]`
to start a native worker; add `--profile room` for an Asha project conversation,
or `--transport structured` for a Claude/Codex result-returning utility.
Session commands include list, show, attach, send, close, stop, resume and doctor.
Each command accepts --help; existing structured request commands remain available.
Structured owners start where work is queued; `session admission
{status|pause|drain|resume|stop}` sets runtime admission for structured work.""", file=stream)


def _room_usage(stream=sys.stdout) -> None:
    print("""asha room: persistent project conversations with the Asha persona

Usage:
  asha room open NAME --project PROJECT --harness H --prompt TEXT [--json]
  asha room list [--json]
  asha room attach NAME|UUID [--json]
  asha room close NAME|UUID [--yes] [--json]

Rooms run detached in the initialized project's canonical checkout. They do
not create a Control workspace. Closing permanently ends the tmux session.""",
          file=stream)


def _run_command_popup(
    adapter: TmuxAdapter,
    config,
    command: list[str],
    attach: str,
    label: str,
    env: Mapping[str, str],
) -> str | None:
    """Use the caller-bound popup seam for an identity-checked command."""
    pane = env.get("TMUX_PANE")
    client = None if not pane else adapter.caller_client(pane)
    if client is None:
        return (
            "asha control: no tmux client is attached to this session; "
            f"attach with: {attach}"
        )
    argv = adapter.popup_command_argv(
        client=client, command=command,
        width=config.popup_width, height=config.popup_height,
    )
    try:
        result = subprocess.run(argv, shell=False, check=False)
    except OSError as exc:
        raise TmuxError(f"tmux popup could not be invoked: {exc}") from exc
    if result.returncode != 0:
        return (
            f"asha control: popup attach failed with status {result.returncode}; "
            f"Room {label} is still registered; retry with: {attach}"
        )
    return None


def _room_parse(args: list[str]) -> tuple[str, list[str], dict[str, Any]]:
    """Strict small parser for the public Room surface."""
    if not args or args[0] in {"-h", "--help", "help"}:
        return "help", [], {}
    command = args[0]
    if command not in {"open", "list", "attach", "close"}:
        raise ValueError(f"unknown room command: {command}")
    positional: list[str] = []
    options: dict[str, Any] = {"json": False, "yes": False}
    value_options = {
        "--project": "project", "--harness": "harness", "--prompt": "prompt",
    }
    index = 1
    seen: set[str] = set()
    while index < len(args):
        argument = args[index]
        if argument in {"--json", "--yes"}:
            key = argument[2:]
            if key in seen:
                raise ValueError(f"{argument} may be specified only once")
            seen.add(key)
            options[key] = True
            index += 1
            continue
        destination = value_options.get(argument)
        if destination is not None:
            if destination in seen:
                raise ValueError(f"{argument} may be specified only once")
            if index + 1 >= len(args):
                raise ValueError(f"{argument} requires a value")
            seen.add(destination)
            options[destination] = args[index + 1]
            index += 2
            continue
        if argument.startswith("--"):
            raise ValueError(f"unknown room argument: {argument}")
        positional.append(argument)
        index += 1
    allowed = {
        "open": {"json", "project", "harness", "prompt"},
        "list": {"json"}, "attach": {"json"}, "close": {"json", "yes"},
    }[command]
    extra = seen - allowed
    if extra:
        raise ValueError(f"room {command} does not accept --{sorted(extra)[0]}")
    expected = 1 if command in {"open", "attach", "close"} else 0
    if len(positional) != expected:
        raise ValueError(
            f"room {command} requires exactly {expected} name or UUID argument(s)"
        )
    if command == "open":
        missing = [key for key in ("project", "harness", "prompt") if key not in options]
        if missing:
            raise ValueError(f"room open requires --{missing[0]}")
    return command, positional, options


def _room_root(env: Mapping[str, str]) -> Path:
    raw = env.get("ASHA_ROOT")
    root = Path(__file__).resolve().parents[2] if raw is None else Path(raw)
    if not root.is_absolute() or root.resolve() != root:
        raise ValueError("ASHA_ROOT must be an exact canonical absolute path")
    return root


def _room_command(args: list[str], env: Mapping[str, str]) -> int:
    from .rooms import RoomStore, attach_room, close_room, control_config, list_rooms, open_room

    command, positional, options = _room_parse(args)
    if command == "help":
        _room_usage()
        return 0
    config = load_config(env)
    if command in {"open", "close"}:
        # Opening or ending a Room is a session operator act (K4).
        from .sessions import refuse_managed_operator
        refuse_managed_operator(control_config(config), env)
    store = RoomStore(config)
    adapter = TmuxAdapter()
    if command == "open":
        payload = open_room(
            name=positional[0], project=options["project"],
            harness=options["harness"], prompt=options["prompt"],
            config=config, env=env, tmux=adapter, asha_root=_room_root(env),
        )
        if options["json"]:
            _json(payload)
        else:
            print(f"Room: {payload['name']} ({payload['room_id']})")
            print(f"Project: {payload['project_name']}  {payload['project_root']}")
            print(f"Harness: {payload['harness']}")
            print(f"Tmux: {payload['session']}:{payload['window']} {payload['pane_id']}")
            print(f"Attach: {payload['attach']}")
        return 0
    if command == "list":
        payload = list_rooms(store, tmux=adapter)
        if options["json"]:
            _json(payload)
        elif not payload["rooms"]:
            print("No Rooms.")
        else:
            for room in payload["rooms"]:
                warning = "  SHARED CHECKOUT" if room["shared_working_tree"] else ""
                print(
                    f"{room['name']:<24} {room['harness']:<8} {room['state']:<11} "
                    f"{room['project_name']}  {room['room_id']}{warning}"
                )
        return 0
    if command == "attach":
        payload = attach_room(store, positional[0], tmux=adapter)
        if options["json"]:
            _json(payload)
            return 0
        if env.get("TMUX"):
            refusal = _run_command_popup(
                adapter, config, payload["attach_argv"], payload["attach"],
                payload["name"], env,
            )
            if refusal is not None:
                print(refusal, file=sys.stderr)
                return 2
        else:
            print(payload["attach"])
        return 0
    assert command == "close"
    if not options["yes"]:
        interactive = (
            not options["json"]
            and getattr(sys.stdin, "isatty", lambda: False)()
            and getattr(sys.stderr, "isatty", lambda: False)()
        )
        if not interactive:
            raise ValueError("room close requires --yes in non-interactive or JSON mode")
        print(
            "Room close permanently ends its tmux session. Type exact yes: ",
            end="", file=sys.stderr, flush=True,
        )
        if sys.stdin.readline().rstrip("\n") != "yes":
            raise ValueError("room close cancelled")
    payload = close_room(store, positional[0], tmux=adapter)
    if options["json"]:
        _json(payload)
    else:
        suffix = " (already closed)" if payload["already_closed"] else ""
        print(f"Closed Room: {payload['name']} ({payload['room_id']}){suffix}")
    return 0


_PROJECTS_USAGE = "Usage: asha control projects [--root DIR]... [--depth N] [--match TEXT] [--json]"


def _projects_command(args: list[str], env: Mapping[str, str]) -> int:
    """List the projects a session or Room can launch in, as the chair resolves them."""
    from .projects import DEFAULT_DEPTH, list_projects_across, resolve_roots
    if args and args[0] in {"-h", "--help", "help"}:
        print(_PROJECTS_USAGE)
        return 0
    options: dict[str, Any] = {"root": [], "depth": None, "match": None, "json": False}
    index = 0
    while index < len(args):
        argument, name = args[index], args[index][2:]
        if argument not in {"--root", "--depth", "--match", "--json"}:
            raise ValueError(f"unknown projects argument: {argument}")
        if name != "root" and options[name] not in {None, False}:
            raise ValueError(f"{argument} may be specified only once")
        if name == "json":
            options["json"], index = True, index + 1
            continue
        if index + 1 >= len(args):
            raise ValueError(f"{argument} requires a value")
        if name == "root":
            options["root"].append(args[index + 1])
        else:
            options[name] = args[index + 1]
        index += 2
    try:
        depth = DEFAULT_DEPTH if options["depth"] is None else int(options["depth"])
    except ValueError as exc:
        raise ValueError("projects --depth must be an integer") from exc
    roots, roots_from = resolve_roots(options["root"], env=env)
    payload = list_projects_across(roots, depth=depth, match=options["match"], source_of_roots=roots_from)
    if options["json"]:
        _json(payload)
        return 0
    width = max([len(entry["name"]) for entry in payload["projects"]], default=0)
    for entry in payload["projects"]:
        print(f"{entry['name']:<{width}}  {entry['root']}")
    if not payload["projects"]:
        print("No projects.")
    for item in payload["skipped"]:
        print(f"skipped {item['root']}: {item['reason']}")
    return 0


def _doctor_command(args: list[str], env: Mapping[str, str]) -> int:
    """Control's own dependency, hook and state probes."""
    from .doctor import run_doctor

    if args and args[0] in {"-h", "--help", "help"}:
        print("Usage: asha control doctor [--json]")
        return 0
    if any(argument != "--json" for argument in args) or len(args) > 1:
        raise ValueError("control doctor accepts only --json")
    payload = run_doctor(load_config(env), env=env)
    if args:
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
            print("asha control core requires the `room`, `initiative` or `control` route", file=sys.stderr)
            return 2
        domain, tail = args[0], args[1:]
        if domain == "migrate":
            print(_MIGRATE_RETIRED, file=sys.stderr)
            return 2
        if domain == "task":
            print(_TASK_RETIRED, file=sys.stderr)
            return 2
        if domain == "room":
            return _room_command(tail, values)
        if domain == "initiative":
            from .initiative_evidence import main as evidence_main
            return evidence_main(tail, env=values)
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
            if tail and tail[0] == "supervisor":
                print(_SUPERVISOR_RETIRED, file=sys.stderr)
                return 0 if tail[1:2] == ["run"] else 2
            if tail and tail[0] == "session":
                from .sessions import main as session_main
                return session_main(tail[1:], env=values)
            if tail and tail[0] == "projects":
                return _projects_command(tail[1:], values)
            if tail and tail[0] == "doctor":
                return _doctor_command(tail[1:], values)
            if not tail:
                from .session_tui import run_tui
                return run_tui(values)
            _control_usage(sys.stderr)
            return 2
        print("unknown Control route", file=sys.stderr)
        return 2
    except (ConfigError, StoreError, TmuxError, ValueError) as exc:
        print(f"asha control: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt as exc:
        guidance = getattr(exc, "asha_room_guidance", None)
        if isinstance(guidance, str) and guidance:
            print(f"asha control: {guidance}", file=sys.stderr)
        print("asha control: interrupted", file=sys.stderr)
        return 130
    except SystemExit as exc:
        guidance = getattr(exc, "asha_room_guidance", None)
        if isinstance(guidance, str) and guidance:
            print(f"asha control: {guidance}", file=sys.stderr)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
