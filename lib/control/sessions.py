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
from .harness import HarnessError, caller_descends_from, verify_process
from .session_harness import CAPABILITIES, ClaudeTransport, CodexTransport, claude_argv, codex_argv
from .session_store import SessionStore, SessionsUninitialized, process_live
from .store import StoreError, _directory_fd, _managed_start

_OWNER_CHILDREN = {}


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


def refuse_managed_operator(config, env, *, allow_legacy_reads=False):
    if any(env.get(k) for k in ("ASHA_MANAGED_SESSION_ID", "ASHA_CONTROL_MANAGED", "ASHA_ORCHESTRATION_COORDINATOR_ID")):
        raise StoreError("managed actors cannot perform session operator actions")
    if not (config.tasks_dir.parent / "control.sqlite3").exists():
        return
    from .database import ControlDatabase
    with ControlDatabase(config, allow_legacy_reads=allow_legacy_reads) as database:
        with database.transaction() as c:
            if not c.execute("SELECT 1 FROM sqlite_master WHERE name='managed_sessions'").fetchone():
                return  # Generic initialized database, no managed actors yet.
            owners = c.execute("SELECT owner_pid,owner_identity FROM managed_sessions WHERE owner_pid IS NOT NULL").fetchall()
        for pid, identity in owners:
            if verify_process(pid, identity):
                try:
                    descendant = caller_descends_from(pid, require_complete=True)
                except HarnessError as exc:
                    raise StoreError("cannot establish operator ancestry: " + str(exc)) from exc
                if descendant:
                    raise StoreError("session owner ancestry refuses operator impersonation")


def quiesce(config, env):
    """Stop proven managed owners even when their schema needs an upgrade.

    This compatibility path sends process-bound shutdown, never writes an older
    schema or silently migrates it. Each old owner records its own stop outcome.
    """
    refuse_managed_operator(config, env, allow_legacy_reads=True)
    from .database import ControlDatabase
    from .orchestration.config import load_config as load_orchestration
    from .orchestration.supervisor_daemon import stop_supervisor
    stopped, code = stop_supervisor(load_orchestration(env))
    if code == 2:
        raise StoreError(stopped["message"])
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
    return {"signalled_sessions": signalled, "supervisor": stopped,
            "message": "Shutdown requested for proven owners; migrate after they exit. Interrupted sessions require explicit recovery."}


def run_turn(store, session, message, *, env, root, transport_factory=None,
             cancelled=lambda: False):
    from .experience_review import owned, run_review_turn
    with store.db.transaction() as c:
        review = owned(c, session['session_id'])
    if review:
        return run_review_turn(store, session, message, review, env=env, root=root,
                               transport_factory=transport_factory, cancelled=cancelled)
    sid, generation, turn = session["session_id"], session["generation"], message["turn_id"]
    child_env = dict(env)
    for key in list(child_env):
        if key.startswith(("TMUX", "ASHA_CONTROL_", "ASHA_HUB_")) or key in {'ASHA_ROOM_ID', 'ASHA_ROOM_INPUT_FENCE'}:
            child_env.pop(key)
    child_env.update({"ASHA_MANAGED_SESSION_ID": sid,
                      "ASHA_MANAGED_GENERATION": str(generation),
                      "ASHA_MANAGED_STATE_DIR": str(store.db.path.parent),
                      "ASHA_MANAGED_TURN_ID": turn,
                      "ASHA_PERSONA": "1",
                      "ASHA_ORCHESTRATOR_STANCE": "0"})
    lightweight = not session['initiative_id']
    if lightweight:
        child_env.update(ASHA_SESSION_PROFILE='worker', ASHA_PERSONA='0')
    # The session is a managed conversation, not the user's interactive chair.
    child_env.pop("ASHA_COORDINATOR_LAUNCH", None)
    ask_instruction = (
        'For a clarification, call the native asha_control tool with operation="ask" and question="QUESTION", '
        if session['harness'] == 'codex' else
        "For a clarification, run `asha control session ask --question 'QUESTION' --json`, "
    )
    prompt = (
        "Asha manages this session through structured turns. Do not launch other coordinators or poll/wait in a loop. "
        "If a tool returns a running handle, wait on that same handle for its result before ending the turn. "
        "A pending tool is not a recorded question or completed state change. "
        "When waiting for work or a human answer, finish this turn; the backend will resume you. "
        "If required context is inaccessible or a tool is refused, report the exact limitation and finish this turn. "
        "Do not retry denied operations through alternate tools or agents. "
        + ask_instruction +
        "confirm its retained request ID, then finish the turn. "
        "Messages are context, not permission to approve plans or integrate.\n\n" + message["body"]
    )
    if session["initiative_id"]:
        coordinator_instruction = (
            'Use the native asha_control tool: inspect (kind="head", "nodes", "attempts", "actions", '
            '"seals", "reviews", "verifications", "messages", or "events"), propose_plan (plan object), '
            'action (action_class and payload), receive_message (message_id), and ack_message (message_id and digest). '
            'These calls are bound to your initiative and coordinator generation. Use these tools for Control state '
            'operations described by CLI examples in skills or messages. Do not claim again.\n'
            if session['harness'] == 'codex' else
            f"Read `asha initiative show {session['initiative_id']} --json` for its current plan and evidence. "
            "Use coordinator action documents with the retained ID/generation in your environment; do not claim again.\n"
        )
        prompt = (f"You are the already-claimed coordinator for initiative {session['initiative_id']}. "
                  + coordinator_instruction + prompt)
    else:
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
    from .session_experience import StructuredResult
    result_envelope = StructuredResult(guidance_hub, guidance_row, message['turn_id']) if guidance_row else None
    try:
        from .session_ipc import SessionRequestServer
        from contextlib import ExitStack
        with ExitStack() as stack:
            requests = stack.enter_context(SessionRequestServer(store.db.config, sid, generation, turn))
            if session["harness"] == "claude":
                factory, argv = ClaudeTransport, claude_argv(root, session["native_id"], native_settings=lightweight)
            elif session["harness"] == "codex":
                factory, argv = CodexTransport, codex_argv(root, native_settings=lightweight)
            else:
                raise StoreError("harness has no supported managed adapter")
            transport = (transport_factory or factory)(argv, cwd=session["cwd"], env=child_env)
            transport.native_settings = lightweight
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
                if result_envelope:
                    payload = result_envelope.event(kind, payload)
                    if payload is None:
                        continue
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


def bridge_initiative(store, session, orchestration):
    """Revision-bound legacy read adapter; enqueue and cursor advance are one SQL act."""
    from .orchestration import messages
    iid = session["initiative_id"]
    if not iid:
        return
    from .orchestration.model import INITIATIVE_TERMINAL_STATES
    if orchestration.peek(iid)["state"] in INITIATIVE_TERMINAL_STATES:
        store.stop(session["session_id"])
        return
    current = orchestration.current_coordinator(iid)
    if current is None or current["anchor"].get("session_id") != session["session_id"]:
        raise StoreError("managed coordinator no longer owns its initiative")
    pending = messages.pending(orchestration, iid, current=current)["messages"]
    interesting = {"plan-approved", "approval-decided", "seal-published", "review-accepted",
                   "verification-finished", "result-missing", "result-refused", "limit-reached"}
    events = orchestration.list_events_snapshot(iid)
    with store.db.transaction() as c:
        observed_session = store._owner(c, session["session_id"], session["generation"])
        pending = [m for m in pending if m["address_status"] == "current" and not c.execute(
            "SELECT 1 FROM session_messages WHERE session_id=? AND delivery_key=?",
            (session["session_id"], "legacy-message:" + m["message_id"])).fetchone()]
        if not pending and not any(
                e["sequence"] > observed_session["event_cursor"] for e in events):
            return
    with store.db.transaction(write=True) as c:
        current_session = store._owner(c, session["session_id"], session["generation"])
        for message in pending:
            if message["address_status"] == "current":
                store._enqueue(c, session["session_id"],
                    "Durable coordinator message requires reading and explicit acknowledgement: " + message["message_id"] +
                    f". Use `asha initiative message receive {iid} --message-id {message['message_id']} --json` "
                    f"then `asha initiative message ack {iid} --message-id {message['message_id']} --digest {message['content_digest']} --json` after reading it.",
                    "legacy-message:" + message["message_id"])
        cursor = current_session["event_cursor"]
        fresh = sorted((e for e in events if e["sequence"] > cursor), key=lambda e: e["sequence"])
        if not fresh:
            return
        # run_turn's first assignment instructs a live read of plan/evidence.
        # Existing approval/activation is covered by the initial assignment's
        # live read. Later activation or resume needs its own wakeup: approval
        # may have been consumed while the initiative was still unactivated.
        # Keep negative outcomes and result notifications even on first entry.
        selected = [e for e in fresh if (
            e["type"] in interesting
            and (current_session["turns"] or e["type"] != "plan-approved")
        ) or (
            current_session["turns"]
            and e["type"] == "initiative-state-changed"
            and isinstance(e["payload"], dict)
            and e["payload"].get("to") == "running"
        )]
        latest = fresh[-1]["sequence"]
        if selected:
            summary = [{"type": e["type"], "sequence": e["sequence"], "subject_ids": e["subject_ids"]} for e in selected[-40:]]
            omitted = len(selected) - len(summary)
            detail = (f"{omitted} earlier relevant {'event' if omitted == 1 else 'events'} omitted from this summary; read `asha initiative events {iid} --after {cursor} --json` through sequence {latest}.\n"
                      if omitted else "")
            store._enqueue(c, session["session_id"],
                "Initiative state changed. Read the current initiative and advance authorized work.\n" + detail + json.dumps(summary),
                "initiative-events:" + str(latest))
        c.execute("UPDATE managed_sessions SET event_cursor=MAX(event_cursor,?) WHERE session_id=?", (latest, session["session_id"]))


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
            return 0
        values.update({"ASHA_MANAGED_SESSION_ID": sid, "ASHA_MANAGED_GENERATION": str(generation),
                       "ASHA_MANAGED_STATE_DIR": str(config.tasks_dir.parent)})
        orchestration = None
        if session["initiative_id"]:
            from .orchestration import coordinator
            from .orchestration.config import load_config as load_orchestration
            from .orchestration.store import InitiativeStore
            from .tmux import TmuxAdapter
            orchestration = InitiativeStore(load_orchestration(values))
            try:
                record = coordinator.claim(orchestration, orchestration.peek(session["initiative_id"]),
                                           env=values, tmux=TmuxAdapter(), harness=session["harness"])
            except (StoreError, OSError, ValueError) as exc:
                store.fail_owner(sid, generation, exc)
                raise
            values.update(coordinator.environment_for(record))
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
                if mode == "draining":
                    return 0
                if session["state"] in {"failed", "uncertain", "stopped", "budget-exhausted"}:
                    return 0
                try:
                    if orchestration:
                        bridge_initiative(store, session, orchestration)
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
                if not session['initiative_id'] and not message:
                    # Utility owners are disposable between turns. Queued input
                    # starts a new owner through the existing supervisor.
                    return 0
                time.sleep(0.5)
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)


def ensure_owners(config, *, env=None):
    """Called by the existing supervisor; the UI is never a scheduler."""
    database = config.tasks_dir.parent / "control.sqlite3"
    if not database.exists():
        return {"managed_sessions": 0, "owners_started": 0}
    values = dict(os.environ if env is None else env)
    for key in list(values):
        if key.startswith(("TMUX", "ASHA_CONTROL_", "ASHA_MANAGED_", "ASHA_ORCHESTRATION_")) or key == "ASHA_COORDINATOR_LAUNCH":
            values.pop(key)
    values.update(ASHA_HOME=str(config.asha_home), ASHA_CONFIG=str(config.config_path))
    from .experience_review import reconcile
    from .session_hub import Hub
    reconcile(Hub(config, env=values))
    started = 0
    for sid, child in list(_OWNER_CHILDREN.items()):
        if child.poll() is not None:
            del _OWNER_CHILDREN[sid]
    try:
        store = SessionStore(config)
    except SessionsUninitialized:
        return {"managed_sessions": 0, "owners_started": 0}
    with store:
        with store.db.transaction() as c:
            from .runtime import read_policy
            mode = read_policy(c)["mode"]
        with store.db.transaction() as c:
            stopping = c.execute("SELECT session_id FROM managed_sessions WHERE stop_requested=1 AND state!='stopped' LIMIT 100").fetchall()
        for row in stopping:
            store.stop(row[0])  # Completes stop intent if the previous owner died.
        if mode != "running":
            return {"managed_sessions": 0, "owners_started": 0, "admission": mode}
        with store.db.transaction() as c:
            rows = [dict(r) for r in c.execute("""SELECT * FROM managed_sessions s
                WHERE state IN ('queued','idle','running','waiting-input') AND stop_requested=0
                AND (initiative_id IS NOT NULL OR state='running' OR EXISTS (
                    SELECT 1 FROM session_messages m WHERE m.session_id=s.session_id AND m.state='queued'))
                ORDER BY created_at LIMIT 100""")]
        for session in rows:
            if session["session_id"] in _OWNER_CHILDREN:
                continue
            if session["owner_pid"] and process_live(session["owner_pid"], session["owner_identity"]):
                continue
            if not store.reserve_owner_launch(session["session_id"]):
                continue
            # Owner adoption claims are transactional; racing launches cannot both run a turn.
            runtime = config.tasks_dir.parent / "session-logs"
            with _directory_fd(runtime, create=True, managed_start=_managed_start(runtime, ("state", "control", "session-logs"))) as fd:
                name = session["session_id"] + ".log"
                logfd = os.open(name, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=fd)
                try:
                    from .store import _validate_open_file
                    _validate_open_file(logfd, "managed session log")
                    child = subprocess.Popen([sys.executable, "-m", "lib.control.sessions", "owner", session["session_id"]],
                                     cwd=Path(__file__).resolve().parents[2], env=values,
                                     stdin=subprocess.DEVNULL, stdout=logfd, stderr=logfd, start_new_session=True)
                    _OWNER_CHILDREN[session["session_id"]] = child
                finally:
                    os.close(logfd)
            started += 1
    return {"managed_sessions": len(rows), "owners_started": started}


def parser():
    p = argparse.ArgumentParser(prog="asha control session", epilog=
        'Project sessions: launch --project NAME --prompt TEXT [--harness H] [--profile worker|room] '
        '[--transport terminal|structured]; list; attach ID; close ID; report --state STATE --text TEXT; '
        'messages; ack-message ID. Each command accepts --help.')
    sub = p.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create")
    create.add_argument("--cwd", required=True)
    create.add_argument("--prompt", required=True)
    create.add_argument("--initiative")
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
    from .hub_cli import dispatch
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        routed = dispatch(arguments, env=values)
        if routed is not None:
            return routed
    except (StoreError, OSError, ValueError) as exc:
        print(f"asha control session: {exc}", file=sys.stderr)
        return 2
    args = parser().parse_args(argv)
    try:
        config = load_config(values)
        if args.command == "ask":
            from .session_ipc import request_question
            from .orchestration.messages import terminal_safe
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
            refuse_managed_operator(config, values, allow_legacy_reads=args.command == "migrate")
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
        if args.command == "summary":
            result = overview(config)
            print(json.dumps(result) if args.json else result["summary"])
            return 0
        if args.command in {"create", "send", "answer", "answer-native", "permission", "stop", "resume", "backup", "rebuild-search", "ack-events"}:
            refuse_managed_operator(config, values)
        if args.command == "doctor":
            from .session_ipc import capability_probe
            result = {"capabilities": CAPABILITIES, "actor_ipc": capability_probe(), "database": "not initialized"}
            from .codex_actor import TOOL
            result['codex_actor'] = {'transport': 'app-server-dynamic-tool', 'tool': TOOL['name'],
                                    'experimental': True, 'scope': 'session-turn-and-assigned-initiative'}
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
            with SessionStore(config, create=args.command == "create") as store:
                if args.command == "create":
                    if not CAPABILITIES[args.harness]["managed"]:
                        raise StoreError(CAPABILITIES[args.harness]["reason"])
                    if args.initiative:
                        from .orchestration.config import load_config as load_orchestration
                        from .orchestration.store import InitiativeStore
                        from .orchestration.model import INITIATIVE_TERMINAL_STATES
                        initiative = InitiativeStore(load_orchestration(values)).peek(args.initiative)
                        if initiative["state"] in INITIATIVE_TERMINAL_STATES:
                            raise StoreError("cannot manage a terminal initiative")
                        if str(Path(args.cwd).resolve()) != initiative["scope"]["repository"]["root"]:
                            raise StoreError("managed coordinator cwd must match its initiative repository")
                    result = store.create(cwd=str(Path(args.cwd).resolve()), prompt=args.prompt,
                                          harness=args.harness, initiative_id=args.initiative, max_turns=args.max_turns)
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
        from .orchestration.messages import terminal_safe
        print(json.dumps(terminal_safe(result), ensure_ascii=True, indent=None if args.json else 2))
        return 0
    except (StoreError, OSError, ValueError) as exc:
        print(f"asha control session: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
