"""Session-first commands; existing structured-session commands remain available."""
from __future__ import annotations
import argparse
import json
import os
import re
import subprocess
import sys
import uuid
import time

from .config import load_config
from .database import DATABASE_NAME
from .session_hub import Hub
from .session_store import identifier
from .store import StoreError

# Verbs whose first positional is a session ID `session list` shows (#113).
SESSION_ID_VERBS = frozenset({'show', 'attach', 'close', 'stop', 'resume', 'send'})
# A prefix of the canonical ID form. Four characters keep a stray digit from
# selecting a session; legacy Room names stay selectable where Rooms are.
_ID_PREFIX = re.compile(r'[0-9a-f-]{4,36}')
_ID_CHARACTERS = re.compile(r'[0-9a-f-]+')


class _UnavailableTerminal:
    """Stands in for a terminal inventory that could not be read."""
    def __getattr__(self, name):
        from .tmux import TmuxError
        def unavailable(*args, **kwargs):
            raise TmuxError('Terminal observation unavailable')
        return unavailable


def terminal_inventory(deadline):
    """One bounded tmux inventory, or a stand-in that refuses, plus its error."""
    from .tmux import BoundedTmux, TmuxAdapter
    try:
        return BoundedTmux(TmuxAdapter(), deadline).inventory(), []
    except (ValueError, OSError) as exc:
        return _UnavailableTerminal(), ['Terminal observation unavailable: ' + str(exc)]


def overview(config, *, env=None, tmux=None, include_closed=False, tmux_errors=(), deadline=None):
    """The session page. ``tmux`` and ``tmux_errors`` pass in an inventory already read."""
    from .rooms import RoomStore, _owned_state
    from .store import SnapshotBudget
    deadline = time.monotonic() + 2 if deadline is None else deadline
    probe_errors = list(tmux_errors)
    if tmux is None:
        tmux, probe_errors = terminal_inventory(deadline)
    hub = Hub(config, env=env, tmux=tmux)
    try:
        page = hub.list(include_closed=include_closed, deadline=deadline)
    except (ValueError, OSError) as exc:
        page = {'rows': [], 'complete': False, 'error': str(exc)}
    rows = page['rows']
    errors = [page['error']] if page.get('error') else []
    errors += probe_errors
    known_rooms = set()
    known_sessions = set()
    if hub.initialized():
        with hub.database() as db, db.transaction() as c:
            records = [json.loads(r[0]) for r in c.execute('SELECT payload FROM hub_sessions LIMIT 10001')]
            if len(records) > 10000:
                errors.append('Session inventory exceeds this view; showing a partial snapshot')
            known_rooms = {room for r in records for room in [r.get('room_id'), *r.get('room_history', [])]}
            known_sessions = {r['session_id'] for r in records}
    try:
        budget = SnapshotBudget(deadline=deadline, limit=256)
        room_store = RoomStore(config)
        room_records = room_store.bounded_snapshots(budget) if include_closed else room_store.bounded_active_snapshots(budget)
        for room in room_records:
            if room['room_id'] in known_rooms or (room['lifecycle'] == 'ended' and not include_closed):
                continue
            state, detail = _owned_state(room, hub.tmux)
            rows.append(dict(session_id=room['room_id'], room_id=room['room_id'], transport='room',
                             name=room['name'], project=room['project_root'], project_name=room['project_name'],
                             profile='room', harness=room['harness'], lifecycle=room['lifecycle'],
                             activity={'open': 'unknown', 'ended': 'exited', 'missing': 'exited'}.get(state, 'unknown'),
                             reason=detail, pending_messages=0, observed_at=None))
        if budget.truncated or budget.unavailable:
            errors.append('Legacy Room observation is incomplete')
    except (ValueError, OSError) as exc:
        errors.append('Rooms: ' + str(exc))
    try:
        from .sessions import overview as managed_overview
        managed = managed_overview(config, deadline=deadline)
        for item in managed.get('pages', {}).get('sessions', {}).get('rows', []):
            if item['session_id'] in known_sessions:
                continue
            rows.append(dict(session_id=item['session_id'], transport='structured', name=item['session_id'][:8],
                             project=item['cwd'], project_name=item['cwd'].rsplit('/', 1)[-1], profile='worker',
                             harness=item['harness'], lifecycle=item['state'],
                             activity='needs-input' if item['waiting_on'] == 'keeper' else item['state'],
                             reason=item['reason'], pending_messages=int(item['has_queued_input']),
                             observed_at=None, initiative_id=item.get('initiative_id')))
        if managed.get('initialized') and not managed.get('complete'):
            errors.append('Managed session observation is incomplete')
    except (ValueError, OSError) as exc:
        errors.append('Managed sessions: ' + str(exc))
    from .session_presentation import present
    rows = [present(row) for row in rows]
    rows.sort(key=lambda row: {'current': 0, 'ended': 1, 'history': 2}[row['group']])
    complete = page['complete'] and not errors
    return {'contract': 'asha.hub-sessions.v1', 'rows': rows, 'complete': complete,
            'errors': errors, 'summary': f"{sum(r['group'] == 'current' for r in rows)} current; {sum(r['group'] == 'ended' for r in rows)} ended; {sum(r['activity'] == 'needs-input' and r['group'] == 'current' for r in rows)} need input"
            + (' (partial observation)' if not complete else '')}


def _listed_matches(config, hub, needle, *, room_names):
    """{ID: label} for each ID `session list` can show that starts with ``needle``.

    Hub sessions, structured conversations and legacy Rooms the hub does not
    own; a hub session's own Room ID is never listed, so it never matches.
    ``room_names`` adds Rooms whose exact name is ``needle``, as attach and close
    have always accepted.
    """
    from .rooms import RoomStore
    prefix = _ID_PREFIX.fullmatch(needle) is not None
    named = lambda room: room_names and room['name'].casefold() == needle
    rooms = [room for room in RoomStore(config).list()
             if named(room) or (prefix and room['room_id'].startswith(needle))]
    found, hub_rooms = {}, set()
    if (prefix or rooms) and (config.tasks_dir.parent / DATABASE_NAME).exists():
        with hub.database() as db, db.transaction() as c:
            tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' "
                                              "AND name IN ('hub_sessions','managed_sessions')")}
            if prefix and 'hub_sessions' in tables:
                for sid, name in c.execute("SELECT session_id,json_extract(payload,'$.name') FROM hub_sessions "
                                           'WHERE substr(session_id,1,?)=?', (len(needle), needle)):
                    found[sid] = name or 'session'
            if prefix and 'managed_sessions' in tables:
                for (sid,) in c.execute('SELECT session_id FROM managed_sessions WHERE substr(session_id,1,?)=?',
                                        (len(needle), needle)):
                    found.setdefault(sid, 'structured session')
            if 'hub_sessions' in tables:
                hub_rooms = {room['room_id'] for room in rooms if c.execute(
                    "SELECT 1 FROM hub_sessions WHERE json_extract(payload,'$.room_id')=? OR EXISTS("
                    "SELECT 1 FROM json_each(payload,'$.room_history') WHERE value=?) LIMIT 1",
                    (room['room_id'], room['room_id'])).fetchone()}
    for room in rooms:
        if named(room) or room['room_id'] not in hub_rooms:
            found.setdefault(room['room_id'], 'Room ' + room['name'])
    return found


def resolve_session(config, hub, selector, *, room_names=False):
    """The full ID for a unique prefix of one `session list` shows (#113).

    A canonical UUID passes through unchanged and behaves exactly as before.
    An unknown or ambiguous selector names the session it could not find.
    """
    try:
        return identifier(selector)
    except StoreError:
        pass
    needle = selector.strip().casefold()
    found = _listed_matches(config, hub, needle, room_names=room_names)
    if len(found) == 1:
        return next(iter(found))
    if found:
        from .text import terminal_safe
        shown = [f'{sid} ({terminal_safe(label[:48])})' for sid, label in sorted(found.items())]
        more = f'; and {len(shown) - 10} more' if len(shown) > 10 else ''
        raise StoreError(f'session prefix {selector!r} is ambiguous; it matches ' + '; '.join(shown[:10]) + more +
                         '; use more characters')
    short = _ID_CHARACTERS.fullmatch(needle) is not None and len(needle) < 4
    raise StoreError(f'session {selector!r} was not found' +
                     ('; an ID prefix needs at least 4 characters' if short else ''))


def expand_selector(argv, *, env):
    """Expand a short session ID right after its verb, where routing reads it (#113)."""
    if len(argv) < 2 or argv[0] not in SESSION_ID_VERBS or argv[1].startswith('-'):
        return argv
    config = load_config(env)
    selected = resolve_session(config, Hub(config, env=env), argv[1], room_names=argv[0] in {'attach', 'close'})
    return [argv[0], selected, *argv[2:]]


def _interactive():
    """Whether this command has a terminal for tmux to attach."""
    return all(getattr(stream, 'isatty', lambda: False)() for stream in (sys.stdin, sys.stdout))


def same_server(tmux, pane, env):
    """Whether the caller runs inside tmux on the server holding ``pane``.

    tmux exports ``TMUX=socket_path,server_pid,session`` to its panes.
    """
    fields = env.get('TMUX', '').rsplit(',', 2)
    if len(fields) != 3 or not fields[0] or not fields[1].isascii() or not fields[1].isdigit():
        return False
    return tmux.server_identity(pane) == (fields[0], int(fields[1]))


def attach_terminal(attached, *, tmux, env):
    """Attach this terminal to a verified Room target; the exit status (#113).

    Inside tmux on the Room's server the caller's own client switches to it;
    anywhere else tmux attaches this terminal, and refuses to nest by itself.
    Both forms keep the fail-closed ownership check of the printed command.
    """
    from .text import terminal_safe
    from .rooms import room_switch_argv
    argv = attached['attach_argv']
    if same_server(tmux, attached['pane_id'], env):
        client = tmux.caller_client(env['TMUX_PANE']) if env.get('TMUX_PANE') else None
        if client is None:
            print('asha control session: no tmux client is showing this pane; attach with: ' + attached['attach'],
                  file=sys.stderr)
            return 2
        argv = room_switch_argv(attached, tmux=tmux, client=client)
    status = subprocess.run(argv, check=False).returncode
    if status == 66:
        print(f"asha control session: Room {terminal_safe(attached['name'])} failed its ownership check; "
              'nothing was attached', file=sys.stderr)
        return 2
    if status != 0:
        print(f"asha control session: tmux exited with status {status}; attach with: {attached['attach']}",
              file=sys.stderr)
        return 2
    return 0


def startup_observation():
    from datetime import datetime, timezone
    stamp = datetime.now(timezone.utc).isoformat(timespec='seconds')
    try:
        snapshot = overview(load_config(os.environ), env=os.environ)
        counts = {profile: sum(r.get('profile') == profile for r in snapshot['rows']) for profile in ('room', 'worker')}
        detail = f"{counts['room']} Rooms; {counts['worker']} workers; " + snapshot['summary']
    except (OSError, ValueError):
        detail = 'Session evidence unavailable; counts unknown.'
    return ('Asha current activity observation\nObserved at: ' + stamp + '; freshness: just observed.\n' + detail +
            '\nCounts are observed lower bounds when the snapshot is partial.' +
            '\nRead-only observation; no execution authority. Summarize briefly, then take the Keeper\'s work.\n')


def dispatch(argv, *, env):
    """None means this is an existing structured-session verb."""
    if not argv:
        return None
    verb = argv[0]
    config = load_config(env)
    hub = Hub(config, env=env)
    if verb == 'experience':
        from .experience_cli import dispatch as experience_dispatch
        return experience_dispatch(hub, argv[1:])
    def is_managed(sid):
        from .session_store import SessionStore, SessionsUninitialized
        try:
            with SessionStore(config) as sessions, sessions.db.transaction() as c:
                return c.execute('SELECT 1 FROM managed_sessions WHERE session_id=?', (sid,)).fetchone() is not None
        except SessionsUninitialized:
            return False
    always = {'launch', 'attach', 'close', 'report', 'event', 'event-lost', 'messages', 'ack-message', 'list',
              'handoff'}
    if verb not in always:
        if verb not in {'show', 'send', 'stop', 'resume'} or len(argv) < 2 or not hub.owns(argv[1]):
            return None
    parser = argparse.ArgumentParser(prog='asha control session ' + verb)
    parser.add_argument('--json', action='store_true')
    if verb == 'launch':
        parser.add_argument('--project', required=True)
        parser.add_argument('--prompt', '--intent', dest='prompt', required=True)
        parser.add_argument('--name')
        parser.add_argument('--harness', default='claude', choices=['claude', 'codex', 'copilot', 'opencode'])
        parser.add_argument('--profile', default='worker', choices=['worker', 'room'])
        parser.add_argument('--session-id')
        parser.add_argument('--transport', default='terminal', choices=['terminal', 'structured'])
        parser.add_argument('--result-contract', choices=['asha.session-result.v1'])
        parser.add_argument('--model', help='native model for this session; omitted means the harness default')
        parser.add_argument('--effort', help='native reasoning effort; omitted means the harness default')
    elif verb in SESSION_ID_VERBS:
        parser.add_argument('session_id', help='a session ID, or a unique prefix of one as `list` shows it')
        if verb == 'attach':
            parser.add_argument('--print', action='store_true',
                                help='print the verified tmux command instead of attaching')
        if verb in {'resume', 'send'}:
            parser.add_argument('--text', required=True)
        if verb == 'send':
            parser.add_argument('--key', default=None)
        if verb == 'resume':
            parser.add_argument('--digest')
        if verb == 'close':
            parser.add_argument('--force', action='store_true', help='a zero wait: terminate now without asking for a save')
            parser.add_argument('--wait', type=int, default=None, metavar='SECONDS',
                                help='how long to wait for a Memory save before terminating '
                                     '(default control.close_wait_seconds, 60)')
    elif verb == 'handoff':
        parser.add_argument('--request', metavar='REQUEST_ID')
        # Accepted and ignored for one release: close requests in live Rooms still name it.
        parser.add_argument('--attempt', help=argparse.SUPPRESS)
        parser.add_argument('--read', action='store_true', help='print live memory destination facts for this session')
        parser.add_argument('--outcome', choices=['no-durable-update', 'failed', 'blocked'])
        parser.add_argument('--detail')
        parser.add_argument('--active-file')
        parser.add_argument('--decisions-file')
        parser.add_argument('--expected-active', metavar='DIGEST')
        parser.add_argument('--expected-decisions', metavar='DIGEST')
    elif verb in {'event', 'report'}:
        parser.add_argument('--event' if verb == 'event' else '--state', required=True)
        if verb == 'event':
            parser.add_argument('--stop-hook-active', action='store_true')
            parser.add_argument('--emitted-at', type=float, help='when the native hook fired (epoch seconds)')
            parser.add_argument('--cwd', help="native hook payload cwd; refused outside the session's project")
            parser.add_argument('--background-tasks', type=int,
                                help='background tasks the native Stop reported still running (#99)')
            parser.add_argument('--source', help='native SessionStart payload source; clear may rebind the session (F6)')
            # Accepted and ignored for one release (D11): hooks in live Rooms still pass them.
            for obsolete in ('--tool-kind', '--tool-token', '--sequence', '--sequence-pane', '--order', '--attempts'):
                parser.add_argument(obsolete, help=argparse.SUPPRESS)
        parser.add_argument('--native-id')
        parser.add_argument('--text')
    elif verb == 'event-lost':
        # The hook bridge's own record of a report it gave up on: a loss metric only.
        parser.add_argument('--event', required=True)
        parser.add_argument('--reason', required=True)
        parser.add_argument('--native-id')
        parser.add_argument('--emitted-at', type=float)
        parser.add_argument('--budget', type=float)
        # Accepted and ignored for one release: the late-Stop delivery it fed is retired.
        parser.add_argument('--background-tasks', type=int, help=argparse.SUPPRESS)
    elif verb == 'messages':
        parser.add_argument('session_id', nargs='?')
        parser.add_argument('--offset', type=int, default=0)
        parser.add_argument('--limit', type=int, default=100)
    elif verb == 'ack-message':
        parser.add_argument('message_id')
        parser.add_argument('--delivery-digest')
    elif verb == 'list':
        parser.add_argument('--all', action='store_true')
    if verb in {'launch', 'resume', 'send'}:
        learning = parser.add_mutually_exclusive_group()
        learning.add_argument('--learning', dest='learning_ids', action='append', default=None)
        learning.add_argument('--no-learning', dest='learning_ids', action='store_const', const=[])
    if verb in {'report', 'handoff'}:
        group = parser.add_mutually_exclusive_group()
        group.add_argument('--experience-file')
        group.add_argument('--experience-ref')
        parser.add_argument('--supersedes')
        parser.add_argument('--key')
    args = parser.parse_args(argv[1:])
    try:
        if verb in SESSION_ID_VERBS:
            args.session_id = resolve_session(config, hub, args.session_id, room_names=verb in {'attach', 'close'})
        if verb == 'launch':
            result = hub.launch(**{k: v for k, v in vars(args).items() if k != 'json'})
        elif verb == 'list':
            result = overview(config, env=env, include_closed=args.all)
        elif verb == 'show':
            result = hub.show(args.session_id, refresh_usage=True)
        elif verb == 'send':
            result = hub.send(args.session_id, args.text, key=args.key or str(uuid.uuid4()), learning_ids=args.learning_ids)
        elif verb in {'stop', 'close'}:
            if verb == 'close' and args.force and args.wait is not None:
                parser.error('--force is a zero wait; it cannot be combined with --wait')
            if hub.owns(args.session_id):
                if verb == 'close':
                    result = hub.close(args.session_id, force=args.force, wait=args.wait)
                else:
                    result = hub.stop(args.session_id)
            else:
                from .sessions import refuse_managed_operator
                refuse_managed_operator(config, env)
                if verb == 'close' and not args.force:
                    raise StoreError('graceful close with a memory handoff is available only for hub project sessions; '
                                     'this legacy Room or managed session has no handoff seam: use `close ID --force` (without --wait) or `stop ID`')
                if is_managed(args.session_id):
                    from .session_store import SessionStore
                    with SessionStore(config) as sessions:
                        result = sessions.stop(args.session_id)
                else:
                    from .rooms import RoomStore, close_room
                    result = close_room(RoomStore(config), args.session_id, tmux=hub.tmux)
        elif verb == 'resume':
            result = hub.resume(args.session_id, prompt=args.text, expected_digest=args.digest, learning_ids=args.learning_ids)
        elif verb == 'attach':
            if hub.owns(args.session_id):
                result = hub.attach(args.session_id)
            else:
                if is_managed(args.session_id):
                    result = {'transport': 'structured', 'session_id': args.session_id}
                else:
                    from .rooms import RoomStore, attach_room
                    result = attach_room(RoomStore(config), args.session_id, tmux=hub.tmux)
            if not args.json:
                if result.get('attach') and not args.print and _interactive():
                    return attach_terminal(result, tmux=hub.tmux, env=env)
                print(result.get('attach', 'Open asha control and press Enter on session ' + args.session_id))
                return 0
        elif verb in {'report', 'event'}:
            if verb == 'report':
                result = hub.report(state=args.state, body=args.text, native_id=args.native_id,
                    experience_file=args.experience_file, experience_ref=args.experience_ref,
                    supersedes=args.supersedes, key=args.key)
            else:
                result = hub.observe(args.event, body=args.text, native_id=args.native_id, cwd=args.cwd,
                                     background_tasks=args.background_tasks, emitted_at=args.emitted_at,
                                     source=args.source)
                if result.get('observation') == 'ignored':
                    from .session_hub import record_loss
                    record_loss(config, env, event=args.event, reason='stale-skip', native_id=args.native_id,
                                emitted_at=args.emitted_at, newest=result.get('native_emitted_at'))
            if verb == 'event':
                # The only instruction this bridge ever carries: a pending close
                # request, returned once as the harness's own Stop decision.
                # A skipped (older) Stop has no delivery effects (D2).
                decision = (hub.stop_decision(result, stop_hook_active=args.stop_hook_active)
                            if args.event == 'turn-stopped' and result.get('observation') != 'ignored' else None)
                print(json.dumps(decision, ensure_ascii=True) if decision else '{}', flush=True)
                if decision:
                    # The line above is the whole answer; a failed confirmation only
                    # means the same request is re-emitted at the next guard-free Stop.
                    try:
                        hub.confirm_delivery(result, receipt=decision.receipt)
                    except (ValueError, OSError, StoreError):
                        pass
                return 0
        elif verb == 'event-lost':
            from .session_hub import EVENTS, LOSS_REASONS, record_loss
            if args.event in EVENTS and args.reason in LOSS_REASONS - {'stale-skip'}:
                record_loss(config, env, event=args.event, reason=args.reason, native_id=args.native_id,
                            emitted_at=args.emitted_at, budget=args.budget)
            print('{}')
            return 0
        elif verb == 'handoff':
            if args.read:
                result = hub.handoff_read()
            else:
                from .session_closure import parse_digest
                expected = {'activeContext.md': parse_digest(args.expected_active),
                            'decisions.md': parse_digest(args.expected_decisions)}
                result = hub.handoff(args.request, outcome=args.outcome, detail=args.detail,
                                     active_file=args.active_file, decisions_file=args.decisions_file, expected=expected,
                                     experience_file=args.experience_file, experience_ref=args.experience_ref, supersedes=args.supersedes, key=args.key)
        elif verb == 'messages':
            sid = args.session_id or hub.actor()['session_id']
            result = hub.message_page(sid, offset=args.offset, limit=args.limit)
        elif verb == 'ack-message':
            result = hub.acknowledge(args.message_id, delivery_digest=args.delivery_digest)
        print(json.dumps(result, ensure_ascii=True, indent=None if args.json else 2))
        return 0
    except (ValueError, OSError, StoreError) as exc:
        if verb == 'event-lost':
            print('{}')
            return 0
        if verb == 'event':
            from .session_hub import record_rejection
            record_rejection(config, env, event=args.event, native_id=args.native_id, error=exc, cwd=args.cwd)
            print('{}')
            return 0
        print('asha control session: ' + str(exc), file=sys.stderr)
        return 2
