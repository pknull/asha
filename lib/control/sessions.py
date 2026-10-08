"""Managed session operator API and independently owned harness event loop."""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from .config import load_config
from .database import DatabaseBusyError
from .harness import SANDBOXED_HARNESSES
from .session_harness import CAPABILITIES, ClaudeTransport, CodexTransport, claude_argv, codex_argv
from .session_store import SessionStore, SessionsUninitialized, process_live
from .store import StoreError, _directory_fd, _managed_start

_OWNER_CHILDREN = {}
# The owner runs with the operator's authority outside any harness sandbox, so
# its interpreter is isolated (-I: no PYTHONPATH, user site or cwd on sys.path)
# and this bootstrap imports Control from the checkout's lib/ alone, as
# lib/control.sh does for the router. Bare -I with -m would lose the package.
_OWNER_PROGRAM = ('import runpy,sys; sys.path.insert(0, sys.argv.pop(1)); '
                  'runpy.run_module("control.sessions", run_name="__main__")')


def overview(config, *, limit=100, deadline=None):
    """One small read projection for the chair and Control; no scheduling."""
    from .runtime import admission
    policy = admission(config)
    empty = {"initialized": False, "counts": {}, "questions": 0, "permissions": 0, "queued": 0,
             "admission": policy, "pages": {}, "complete": False, "count_kind": "unknown",
             "observed_at": time.time(), "recovery_counts": {}}
    if not (config.tasks_dir.parent / "control.sqlite3").exists():
        return {**empty, "summary": "No managed sessions"}
    try:
        store = SessionStore(config)
    except SessionsUninitialized:
        return {**empty, "summary": f"Managed sessions not initialized; runtime {policy['mode']}"}
    from .session_activity import summary
    with store:
        return summary(store, limit=limit, deadline=deadline)


def refuse_managed_operator(config, env):
    """Refuse the operator verbs to managed actors and workers by environment label.

    Process ancestry is not consulted: the local user is trusted (threat model,
    2026-10-05), and a sandboxed caller already cannot write Control state.
    """
    if any(env.get(k) for k in ("ASHA_MANAGED_SESSION_ID", "ASHA_CONTROL_MANAGED", "ASHA_ORCHESTRATION_COORDINATOR_ID")):
        raise StoreError("managed actors cannot perform session operator actions")
    # K4 (2026-10-05): a worker on any harness, or a non-chair session on a
    # sandboxed harness, is not the operator. Launch, send and the operator verbs
    # would let it start an unsandboxed session or type into another one. The
    # chair is exempt on every harness: a sandboxed chair reaches these verbs
    # only through an escalation the Keeper approves natively. A Room on an
    # unsandboxed harness already holds native permissions; it is not refused.
    profile = env.get("ASHA_SESSION_PROFILE")
    if profile == "worker":
        raise StoreError("worker sessions cannot perform session operator actions")
    if env.get("ASHA_HARNESS") in SANDBOXED_HARNESSES and profile != "chair":
        raise StoreError(f"non-chair sessions on the sandboxed {env['ASHA_HARNESS']} harness "
                         "cannot perform session operator actions")


def quiesce(config, env):
    """Stop proven managed owners even when their schema needs an upgrade.

    This compatibility path sends process-bound shutdown, never writes an older
    schema or silently migrates it. Each old owner records its own stop outcome.
    """
    refuse_managed_operator(config, env)
    from .database import ControlDatabase
    with ControlDatabase(config, allow_legacy_reads=True) as db:
        with db.transaction() as c:
            owners = c.execute("SELECT session_id,owner_pid,owner_identity FROM managed_sessions WHERE owner_pid IS NOT NULL").fetchall() if c.execute("SELECT 1 FROM sqlite_master WHERE name='managed_sessions'").fetchone() else []
    signalled = []
    for sid, pid, identity in owners:
        if not process_live(pid, identity):
            continue
        try:
            handle = os.pidfd_open(pid)
        except ProcessLookupError:
            continue
        try:
            if not process_live(pid, identity):
                continue
            if Path(f"/proc/{pid}").stat().st_uid != os.geteuid():
                raise StoreError("managed owner belongs to another user")
            signal.pidfd_send_signal(handle, signal.SIGTERM)
            signalled.append(sid)
        finally:
            os.close(handle)
    return {"signalled_sessions": signalled,
            "message": "Shutdown requested for proven owners; migrate after they exit. Interrupted sessions require explicit recovery."}


def run_turn(store, session, message, *, env, root, transport_factory=None,
             cancelled=lambda: False):
    sid, generation, turn = session["session_id"], session["generation"], message["turn_id"]
    child_env = dict(env)
    for key in list(child_env):
        if key.startswith(("TMUX", "ASHA_CONTROL_", "ASHA_HUB_")) or key in {'ASHA_ROOM_ID', 'ASHA_ROOM_INPUT_FENCE'}:
            child_env.pop(key)
    child_env.update({"ASHA_MANAGED_SESSION_ID": sid,
                      "ASHA_MANAGED_GENERATION": str(generation),
                      "ASHA_MANAGED_STATE_DIR": str(store.db.path.parent),
                      "ASHA_MANAGED_TURN_ID": turn,
                      "ASHA_ORCHESTRATOR_STANCE": "0",
                      "ASHA_SESSION_PROFILE": "worker",
                      "ASHA_PERSONA": "0"})
    # The session is a managed conversation, not the user's interactive chair.
    child_env.pop("ASHA_COORDINATOR_LAUNCH", None)
    ask_instruction = (
        'For a clarification, call the native asha_control tool with operation="ask" and question="QUESTION", '
        if session['harness'] == 'codex' else
        "For a clarification, run `asha control session ask --question 'QUESTION' --json`, "
    )
    prompt = message['body'] + '\n\nIf clarification is needed, ' + ask_instruction + 'then return. Otherwise do the assignment and return the result.'
    from .session_hub import Hub
    from .session_guidance import delivery
    guidance_hub = Hub(store.db.config, env=env)
    guidance_manifest = None
    guidance_row = None
    if guidance_hub.owns(sid):
        guidance_row = guidance_hub.get(sid)
        rendered, guidance_manifest = delivery(guidance_hub, guidance_row, message['delivery_key'], message['body'])
        guidance_row = guidance_hub._update(sid, expected_generation=guidance_row['generation'], current_assignment=rendered)
        if guidance_manifest:
            prompt = prompt.replace(message['body'], rendered, 1)
        from .session_completion import WORKER_INSTRUCTION
        prompt += '\n\n' + WORKER_INSTRUCTION
    success = False
    reason = None
    transport = None
    try:
        from .session_ipc import SessionRequestServer
        from contextlib import ExitStack
        with ExitStack() as stack:
            requests = stack.enter_context(SessionRequestServer(store.db.config, sid, generation, turn))
            # Hub launch-time selection (#95), re-applied on every turn and resume.
            from .session_selection import requested
            selection = requested((guidance_row or {}).get('spec'))
            if session["harness"] == "claude":
                factory, argv = ClaudeTransport, claude_argv(root, session["native_id"], native_settings=True,
                                                             selection=selection)
            elif session["harness"] == "codex":
                factory, argv = CodexTransport, codex_argv(root, native_settings=True)
            else:
                raise StoreError("harness has no supported managed adapter")
            transport = (transport_factory or factory)(argv, cwd=session["cwd"], env=child_env)
            transport.native_settings = True
            transport.selection = selection
            if session['harness'] == 'codex':
                from .codex_actor import CodexActor
                transport.actor = stack.enter_context(CodexActor(store.db.config, sid, generation, turn, env=child_env))
            transport.on_spawn = lambda pid: store.bind_provider(sid, generation, turn, pid)
            from .native_requests import NativeRequests
            native = NativeRequests(store)
            transport.native_id = session["native_id"]
            transport.message_id = message.get("message_id", turn)
            transport.open_request = lambda request_id, payload: native.open(sid, generation, turn, request_id, payload)
            transport.cancel_request = lambda request_id, **evidence: native.cancel(sid, generation, turn, request_id, **evidence)
            transport.poll_responses = lambda: native.claim_responses(sid, generation, turn)
            transport.response_submitted = lambda request_id: native.submitted(sid, generation, turn, request_id)
            from .runtime import connection_admission
            def should_stop():
                requests.check()
                return cancelled() or bool(store.get(sid)["stop_requested"]) or connection_admission(store.db)["mode"] == "stopped"
            for kind, payload in transport.events(prompt, cancelled=should_stop):
                store.observe(sid, generation, turn, kind, payload)
                if kind == 'progress' and payload.get('subtype') == 'native-input-acknowledged':
                    from .session_hub import Hub
                    from .session_guidance import supplied
                    hub = Hub(store.db.config, env=env)
                    if hub.owns(sid):
                        supplied(hub, guidance_row or hub.get(sid), message['delivery_key'], guidance_manifest)
                if kind in {"completed", "failed"}:
                    success = kind == "completed"
                    reason = payload.get("reason")
            requests.check()
    except Exception as exc:
        # No automatic replay after ambiguous provider submission.
        try:
            store.finish(sid, generation, turn, success=False, reason=str(exc)[:1000],
                         input_not_submitted=transport is None or getattr(transport, "input_not_submitted", False))
        except StoreError:
            pass  # Preserve the transport/storage failure that made custody uncertain.
        raise
    store.finish(sid, generation, turn, success=success, reason=reason if success else reason or "no terminal result")


def run_owner(config, sid, *, env=None, once=False, transport_factory=None):
    from .runtime import admission
    values = dict(os.environ if env is None else env)
    root = Path(__file__).resolve().parents[2]
    with SessionStore(config) as store:
        mode = admission(config)["mode"]
        if mode != "running":
            if mode == "stopped":
                store.stop(sid)
            return 0
        session = store.claim_owner(sid)
        generation = session["generation"]
        if session["stop_requested"]:
            store.stopped(sid, generation)
            return 0
        if session["state"] in {"failed", "uncertain", "stopped", "budget-exhausted"}:
            # Nothing runs until an explicit recovery, which starts a new owner.
            store.release_owner(sid, generation)
            return 0
        if session["initiative_id"]:
            # Initiatives are retired (L-b); a session bound to one is stopped,
            # as the retired bridge stopped it once its initiative was terminal.
            # This owner holds the claim, so it records the stop itself.
            store.stop(sid)
            store.stopped(sid, generation)
            return 0
        values.update({"ASHA_MANAGED_SESSION_ID": sid, "ASHA_MANAGED_GENERATION": str(generation),
                       "ASHA_MANAGED_STATE_DIR": str(config.tasks_dir.parent)})
        stopping = False

        def stop(_signal, _frame):
            nonlocal stopping
            stopping = True

        previous = {}
        if not once:
            previous = {s: signal.signal(s, stop) for s in (signal.SIGTERM, signal.SIGINT)}
        try:
            while True:
                session = store.get(sid)
                mode = admission(config)["mode"]
                if stopping or session["stop_requested"] or mode == "stopped":
                    store.stopped(sid, generation)
                    return 0
                message = None
                if mode == "running" and session["state"] not in {"failed", "uncertain", "stopped", "budget-exhausted"}:
                    try:
                        message = store.claim_turn(sid, generation)
                    except DatabaseBusyError:
                        # These short transactions roll back on contention. No
                        # provider has received this turn: retry only admission,
                        # never a run_turn whose submission could be ambiguous.
                        if once:
                            return 0
                        time.sleep(0.5)
                        continue
                    except StoreError as exc:
                        try:
                            store.fail_owner(sid, generation, exc)
                        except StoreError:
                            pass  # A replaced owner cannot change its successor.
                        raise
                if message:
                    try:
                        run_turn(store, store.get(sid), message, env=values, root=root,
                                 transport_factory=transport_factory, cancelled=lambda: stopping)
                    except Exception:
                        if stopping or store.get(sid)["stop_requested"] or admission(config)["mode"] == "stopped":
                            store.stopped(sid, generation)
                            return 0
                        raise
                if once:
                    return 0
                # Utility owners are disposable between turns, and nothing
                # schedules them: custody ends only where no input is runnable,
                # so later input starts a new owner where it is queued.
                if not message:
                    try:
                        if store.release_owner(sid, generation):
                            return 0
                    except DatabaseBusyError:
                        pass  # Still the owner; dying here could strand queued input.
                time.sleep(0.5)
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)


def _launch_owner(config, sid, env):
    """Start one detached owner; it outlives the CLI or UI that queued the work."""
    runtime = config.tasks_dir.parent / "session-logs"
    with _directory_fd(runtime, create=True, managed_start=_managed_start(runtime, ("state", "control", "session-logs"))) as fd:
        logfd = os.open(sid + ".log", os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        try:
            from .store import _validate_open_file
            _validate_open_file(logfd, "managed session log")
            # Kept only so a long-lived caller (the dashboard) reaps its exited owners.
            root = Path(__file__).resolve().parents[2]
            _OWNER_CHILDREN[sid] = subprocess.Popen(
                [sys.executable, "-B", "-I", "-c", _OWNER_PROGRAM, str(root / "lib"), "owner", sid],
                cwd=root, env=env,
                stdin=subprocess.DEVNULL, stdout=logfd, stderr=logfd, start_new_session=True)
        finally:
            os.close(logfd)


def ensure_owners(config, *, env=None, session_id=None):
    """Start a detached owner for each structured session with work and no live owner.

    No daemon schedules owners (the supervisor retired 2026-10-07, N1). This runs
    where work is queued (launch, send, resume, answer, admission resume) and
    when an operator runs `session show` or `session list`, which restarts an
    owner lost to a crash or reboot. ``session_id`` limits it to one session.
    The launch reservation admits one of any racing starts; an owner's claim
    fences the rest. The UI is never a scheduler.
    """
    database = config.tasks_dir.parent / "control.sqlite3"
    if not database.exists():
        return {"managed_sessions": 0, "owners_started": 0}
    values = dict(os.environ if env is None else env)
    for key in list(values):
        # The owner is no Room, hub session or managed actor of its caller, and
        # the caller's Python startup variables reach neither it nor its harness.
        if (key.startswith(("TMUX", "ASHA_CONTROL_", "ASHA_MANAGED_", "ASHA_ORCHESTRATION_", "ASHA_HUB_", "PYTHON"))
                or key in {"ASHA_COORDINATOR_LAUNCH", "ASHA_ROOM_ID", "ASHA_ROOM_INPUT_FENCE"}):
            values.pop(key)
    values.update(ASHA_HOME=str(config.asha_home), ASHA_CONFIG=str(config.config_path))
    started = 0
    for sid, child in list(_OWNER_CHILDREN.items()):
        if child.poll() is not None:
            del _OWNER_CHILDREN[sid]
    try:
        store = SessionStore(config)
    except SessionsUninitialized:
        return {"managed_sessions": 0, "owners_started": 0}
    scope, arguments = ("", ()) if session_id is None else (" AND session_id=?", (session_id,))
    with store:
        with store.db.transaction() as c:
            from .runtime import read_policy
            mode = read_policy(c)["mode"]
        with store.db.transaction() as c:
            stopping = c.execute("SELECT session_id FROM managed_sessions WHERE stop_requested=1 AND state!='stopped'"
                                 + scope + " LIMIT 100", arguments).fetchall()
        for row in stopping:
            store.stop(row[0])  # Completes stop intent if the previous owner died.
        if mode != "running":
            return {"managed_sessions": 0, "owners_started": 0, "admission": mode}
        # A lost owner's running turn needs a new owner to reconcile it; queued
        # input behind an open question waits for its answer, not an owner.
        with store.db.transaction() as c:
            rows = [dict(r) for r in c.execute("""SELECT * FROM managed_sessions s
                WHERE state IN ('queued','idle','running','waiting-input') AND stop_requested=0
                AND initiative_id IS NULL AND (state='running' OR (EXISTS (
                    SELECT 1 FROM session_messages m WHERE m.session_id=s.session_id AND m.state='queued')
                    AND NOT EXISTS (SELECT 1 FROM session_requests r WHERE r.session_id=s.session_id AND r.state='pending')))"""
                + scope + " ORDER BY created_at LIMIT 100", arguments)]
        for session in rows:
            if session["owner_pid"] and process_live(session["owner_pid"], session["owner_identity"]):
                continue
            if not store.reserve_owner_launch(session["session_id"]):
                continue
            _launch_owner(config, session["session_id"], values)
            started += 1
    return {"managed_sessions": len(rows), "owners_started": started}


def wake(config, session_id=None, *, env=None):
    """Start owners now; the failure text when they cannot start, else None."""
    try:
        ensure_owners(config, env=env, session_id=session_id)
    except (StoreError, OSError, ValueError) as exc:
        return "Owner could not start: " + str(exc)[:500]
    return None


def restart_missing_owners(config, *, env, session_id=None):
    """`session show` and `session list`: restart owners lost to a crash or reboot.

    Only for an operator caller. A worker or managed actor reads without
    starting anything, as it may not launch or send either.
    """
    try:
        refuse_managed_operator(config, env)
    except StoreError:
        return None
    warning = wake(config, session_id, env=env)
    if warning:
        print("asha control session: " + warning, file=sys.stderr)
    return warning


def parser():
    p = argparse.ArgumentParser(prog="asha control session", epilog=
        'Project sessions: launch --project NAME --prompt TEXT [--harness H] [--profile worker|room] '
        '[--transport terminal|structured]; list; attach ID; close ID; report --state STATE --text TEXT; '
        'messages; ack-message ID. Each command accepts --help.')
    sub = p.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create")
    create.add_argument("--cwd", required=True)
    create.add_argument("--prompt", required=True)
    create.add_argument("--harness", choices=list(CAPABILITIES), default="claude")
    create.add_argument("--max-turns", type=int, default=12)
    for verb in ("show", "stop", "owner", "resume"):
        sub.add_parser(verb).add_argument("session_id")
    sub.add_parser("list")
    sub.add_parser("doctor")
    sub.add_parser("init")
    sub.add_parser("migrate")
    sub.add_parser("quiesce")
    sub.add_parser("rebuild-search")
    sub.add_parser("summary")
    sub.add_parser("admission", help="runtime admission for structured work").add_argument(
        "action", choices=("status", "pause", "drain", "resume", "stop"))
    current = sub.add_parser("current")
    current.add_argument("--kind", choices=("sessions", "requests", "deliveries"), default="sessions")
    current.add_argument("--after")
    current.add_argument("--limit", type=int, default=100)
    sub.add_parser("request").add_argument("request_id")
    backup = sub.add_parser("backup")
    backup.add_argument("destination")
    restore = sub.add_parser("restore")
    restore.add_argument("source")
    search = sub.add_parser("search")
    search.add_argument("text")
    search.add_argument("--session")
    search.add_argument("--after", type=int, default=0)
    search.add_argument("--limit", type=int, default=50)
    events = sub.add_parser("events")
    events.add_argument("session_id")
    events.add_argument("--consumer")
    events.add_argument("--after", type=int)
    events.add_argument("--limit", type=int, default=100)
    acknowledge = sub.add_parser("ack-events")
    acknowledge.add_argument("session_id")
    acknowledge.add_argument("--consumer", required=True)
    acknowledge.add_argument("--through", type=int, required=True)
    resume = sub.choices["resume"]
    resume.add_argument("--text", required=True)
    resume.add_argument("--digest", required=True)
    resume.add_argument("--max-turns", type=int)
    resume.add_argument("--quota-reset-override", metavar="REASON", help="explicitly disregard a retained quota reset time; record why quota is available")
    for verb in ("show", "list"):
        sub.choices[verb].add_argument("--after", type=int, default=0)
        sub.choices[verb].add_argument("--limit", type=int, default=100)
    send = sub.add_parser("send")
    send.add_argument("session_id")
    send.add_argument("--text", required=True)
    send.add_argument("--key", required=True)
    ask = sub.add_parser("ask")
    ask.add_argument("--question", required=True)
    ask.add_argument("--request-id", default=None)
    answer = sub.add_parser("answer")
    answer.add_argument("request_id")
    answer.add_argument("--text", required=True)
    answer.add_argument("--digest", required=True)
    native_answer = sub.add_parser("answer-native")
    native_answer.add_argument("request_id")
    native_answer.add_argument("--answers", required=True, help='JSON object: {"answers":{"question-id":{"answers":["text"]}}}')
    native_answer.add_argument("--digest", required=True)
    permission = sub.add_parser("permission")
    permission.add_argument("request_id")
    permission.add_argument("--decision", choices=("allow", "deny"), required=True)
    permission.add_argument("--digest", required=True)
    permission.add_argument("--reason", default="Operator decision")
    for command in sub.choices.values():
        command.add_argument("--json", action="store_true")
    return p


def main(argv=None, *, env=None):
    values = dict(os.environ if env is None else env)
    from .hub_cli import dispatch, expand_selector
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        # A short ID from `session list` routes like its full ID (#113).
        arguments = expand_selector(arguments, env=values)
        routed = dispatch(arguments, env=values)
        if routed is not None:
            return routed
    except (StoreError, OSError, ValueError) as exc:
        print(f"asha control session: {exc}", file=sys.stderr)
        return 2
    args = parser().parse_args(arguments)
    try:
        config = load_config(values)
        if args.command == "ask":
            from .session_ipc import request_question
            from .text import terminal_safe
            if values.get("ASHA_MANAGED_STATE_DIR") != str(config.tasks_dir.parent):
                raise StoreError("managed actor selected a different state root")
            result = request_question(config, session_id=values.get("ASHA_MANAGED_SESSION_ID"),
                turn_id=values.get("ASHA_MANAGED_TURN_ID"),
                generation=int(values.get("ASHA_MANAGED_GENERATION", "0")),
                question=args.question, request_id=args.request_id)
            print(json.dumps(terminal_safe(result), ensure_ascii=True, indent=None if args.json else 2))
            return 0
        if args.command == "quiesce":
            print(json.dumps(quiesce(config, values)))
            return 0
        if args.command in {"migrate", "restore"}:
            refuse_managed_operator(config, values)
            from .database import ControlDatabase
            if args.command == "restore":
                result = {"restored": str(ControlDatabase.restore(config, Path(args.source))), "admission": "paused"}
            else:
                with ControlDatabase(config, allow_legacy_reads=True) as database:
                    with database.transaction() as c:
                        if c.execute("SELECT 1 FROM sqlite_master WHERE name='managed_sessions'").fetchone():
                            owners = c.execute("SELECT owner_pid,owner_identity FROM managed_sessions WHERE owner_pid IS NOT NULL").fetchall()
                            providers = c.execute("SELECT provider_pid,provider_identity FROM session_turns WHERE provider_pid IS NOT NULL").fetchall()
                            if any(process_live(pid, identity) for pid, identity in [*owners, *providers]):
                                raise StoreError("stop managed session owners before migrating the schema")
                with ControlDatabase(config, migrate=True) as database:
                    result = database.health()
            print(json.dumps(result))
            return 0
        if args.command == "init":
            refuse_managed_operator(config, values)
            with SessionStore(config, create=True) as store:
                result = store.db.health()
            print(json.dumps(result))
            return 0
        if args.command == "owner":
            refuse_managed_operator(config, values)
            return run_owner(config, args.session_id, env=values)
        if args.command == "admission":
            from .runtime import admission, set_admission
            if args.action == "status":
                result = admission(config)
            else:
                refuse_managed_operator(config, values)
                result = set_admission(config, {"pause": "paused", "drain": "draining",
                                                "resume": "running", "stop": "stopped"}[args.action])
                if args.action == "resume":
                    # Work queued while admission was closed has no owner yet.
                    result["owner_warning"] = wake(config, env=values)
            print(json.dumps(result) if args.json else result["message"])
            return 0
        if args.command == "summary":
            result = overview(config)
            print(json.dumps(result) if args.json else result["summary"])
            return 0
        if args.command in {"create", "send", "answer", "answer-native", "permission", "stop", "resume", "backup", "rebuild-search", "ack-events"}:
            refuse_managed_operator(config, values)
        if args.command == "doctor":
            result = {"capabilities": CAPABILITIES, "database": "not initialized"}
            from .codex_actor import TOOL
            result['codex_actor'] = {'transport': 'app-server-dynamic-tool', 'tool': TOOL['name'],
                                    'experimental': True, 'scope': 'session-turn'}
            from .session_output import MAX_BYTES, MAX_RECORDS, MAX_CONSUMERS
            result["session_output"] = {"max_payload_record_bytes_per_session": MAX_BYTES, "max_records_per_session": MAX_RECORDS,
                                        "max_consumers_per_session": MAX_CONSUMERS,
                                        "audit_events": "retained", "gaps": "explicit", "consumer_cursors": "durable",
                                        "legacy_inline_output": "preserved"}
            if (config.tasks_dir.parent / "control.sqlite3").exists():
                from .database import ControlDatabase
                with ControlDatabase(config) as database:
                    result["database"] = database.health()
                result["sessions_initialized"] = overview(config)["initialized"]
        elif args.command in {"backup", "rebuild-search"}:
            from .database import ControlDatabase
            with ControlDatabase(config) as database:
                if args.command == "backup":
                    result = {"backup": str(database.backup(Path(args.destination)))}
                else:
                    database.rebuild_search()
                    result = {"search": "rebuilt"}
        else:
            if args.command in {"list", "show"}:
                restart_missing_owners(config, env=values, session_id=getattr(args, "session_id", None))
            with SessionStore(config, create=args.command == "create") as store:
                if args.command == "create":
                    if not CAPABILITIES[args.harness]["managed"]:
                        raise StoreError(CAPABILITIES[args.harness]["reason"])
                    result = store.create(cwd=str(Path(args.cwd).resolve()), prompt=args.prompt,
                                          harness=args.harness, max_turns=args.max_turns)
                elif args.command in {"list", "show"}:
                    result = store.snapshot(getattr(args, "session_id", None), after=args.after, limit=args.limit)
                elif args.command == "search":
                    result = store.search(args.text, session_id=args.session, after=args.after, limit=args.limit)
                elif args.command == "events":
                    result = store.events(args.session_id, consumer=args.consumer, after=args.after, limit=args.limit)
                elif args.command == "ack-events":
                    result = store.acknowledge_events(args.session_id, args.consumer, args.through)
                elif args.command == "request":
                    from .native_requests import NativeRequests
                    result = store.get_request(args.request_id)
                    if result["kind"] in {"native-permission", "native-clarification"}:
                        result = NativeRequests(store).get(args.request_id)
                elif args.command == "current":
                    result = store.current_work(kind=args.kind, after=args.after, limit=args.limit)
                elif args.command == "resume":
                    result = store.resume(args.session_id, prompt=args.text,
                                          expected_digest=args.digest, max_turns=args.max_turns,
                                          quota_reset_override=args.quota_reset_override)
                elif args.command == "send":
                    result = store.enqueue(args.session_id, args.text, key=args.key)
                elif args.command == "stop":
                    store.stop(args.session_id)
                    result = store.get(args.session_id)
                elif args.command == "answer":
                    result = store.answer(args.request_id, args.text, expected_digest=args.digest)
                elif args.command == "permission":
                    from .native_requests import NativeRequests
                    result = NativeRequests(store).decide(args.request_id, args.decision,
                        expected_digest=args.digest, reason=args.reason)
                elif args.command == "answer-native":
                    from .native_requests import NativeRequests
                    from .session_harness import _unique_object
                    answers = json.loads(args.answers, object_pairs_hook=_unique_object)
                    result = NativeRequests(store).answer_native(args.request_id, answers,
                        expected_digest=args.digest)
            if args.command in {"create", "send", "resume", "answer"}:
                # Queued work starts its owner here; no daemon will later.
                warning = wake(config, result["session_id"], env=values)
                if warning:
                    print("asha control session: " + warning, file=sys.stderr)
        from .text import terminal_safe
        print(json.dumps(terminal_safe(result), ensure_ascii=True, indent=None if args.json else 2))
        return 0
    except (StoreError, OSError, ValueError) as exc:
        print(f"asha control session: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
