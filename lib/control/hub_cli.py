"""Session-first commands; existing structured-session commands remain available."""
from __future__ import annotations
import argparse
import json
import os
import sys
import uuid
import time

from .config import load_config
from .session_hub import Hub
from .store import StoreError


def overview(config, *, env=None, tmux=None, include_closed=False):
    from .rooms import RoomStore, _owned_state
    from .store import SnapshotBudget
    from .tmux import TmuxAdapter, TmuxError
    from .orchestration.observation import BoundedTmux
    deadline = time.monotonic() + 2
    probe_errors = []
    if tmux is None:
        try:
            tmux = BoundedTmux(TmuxAdapter(), deadline).inventory()
        except (ValueError, OSError) as exc:
            probe_errors.append('Terminal observation unavailable: ' + str(exc))
            class UnavailableTerminal:
                def __getattr__(self, name):
                    def unavailable(*args, **kwargs):
                        raise TmuxError('Terminal observation unavailable')
                    return unavailable
            tmux = UnavailableTerminal()
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
    failed_closes = sum(bool((r.get('closure') or {}).get('needs_attention')) for r in rows)
    return {'contract': 'asha.hub-sessions.v1', 'rows': rows, 'complete': complete,
            'errors': errors, 'summary': f"{sum(r['group'] == 'current' for r in rows)} current; {sum(r['group'] == 'ended' for r in rows)} ended; {sum(r['activity'] == 'needs-input' and r['group'] == 'current' for r in rows)} need input"
            + (f"; {failed_closes} closes need attention" if failed_closes else '') + (' (partial observation)' if not complete else '')}


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
    always = {'launch', 'attach', 'close', 'report', 'event', 'messages', 'ack-message', 'list', 'handoff'}
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
    elif verb in {'show', 'attach', 'close', 'stop', 'resume', 'send'}:
        parser.add_argument('session_id')
        if verb in {'resume', 'send'}:
            parser.add_argument('--text', required=True)
        if verb == 'send':
            parser.add_argument('--key', default=None)
        if verb == 'resume':
            parser.add_argument('--digest')
        if verb == 'close':
            parser.add_argument('--force', action='store_true', help='terminate now; no memory handoff is requested or claimed')
            parser.add_argument('--wait', type=int, default=0, metavar='SECONDS', help='poll for the handoff acknowledgement before terminating')
    elif verb == 'handoff':
        parser.add_argument('--request', metavar='REQUEST_ID')
        parser.add_argument('--attempt', type=int)
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
            parser.add_argument('--tool-kind', choices=['work', 'report', 'finalizer'], default='work')
            parser.add_argument('--tool-token', default='unknown')
        parser.add_argument('--native-id')
        parser.add_argument('--text')
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
        if verb == 'launch':
            result = hub.launch(**{k: v for k, v in vars(args).items() if k != 'json'})
        elif verb == 'list':
            result = overview(config, env=env, include_closed=args.all)
        elif verb == 'show':
            result = hub.show(args.session_id)
        elif verb == 'send':
            result = hub.send(args.session_id, args.text, key=args.key or str(uuid.uuid4()), learning_ids=args.learning_ids)
        elif verb in {'stop', 'close'}:
            if hub.owns(args.session_id):
                if verb == 'close':
                    result = hub.close(args.session_id, force=args.force, wait=args.wait)
                else:
                    result = hub.stop(args.session_id)
            else:
                from .sessions import refuse_managed_operator
                refuse_managed_operator(config, env)
                if verb == 'close' and (not args.force or args.wait):
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
                print(result.get('attach', 'Open asha control and press Enter on session ' + args.session_id))
                return 0
        elif verb in {'report', 'event'}:
            if verb == 'report':
                result = hub.report(state=args.state, body=args.text, native_id=args.native_id,
                    experience_file=args.experience_file, experience_ref=args.experience_ref,
                    supersedes=args.supersedes, key=args.key)
            else:
                result = hub.observe(args.event, body=args.text, native_id=args.native_id,
                                     tool_kind=args.tool_kind, tool_token=args.tool_token)
            if verb == 'event':
                # The only instruction this bridge ever carries: a pending close
                # request, returned once as the harness's own Stop decision.
                decision = hub.stop_decision(result, stop_hook_active=args.stop_hook_active) if args.event == 'turn-stopped' else None
                print(json.dumps(decision, ensure_ascii=True) if decision else '{}', flush=True)
                if decision:
                    # The line above is the whole answer; a failed confirmation only
                    # means the same request is re-emitted at the next guard-free Stop.
                    try:
                        hub.confirm_delivery(result, receipt=decision.receipt)
                    except (ValueError, OSError, StoreError):
                        pass
                return 0
        elif verb == 'handoff':
            if args.read:
                result = hub.handoff_read()
            else:
                if args.request and args.attempt is None:
                    parser.error('--request requires --attempt from the delivered close request')
                if not args.request and args.attempt is not None:
                    parser.error('--attempt requires --request')
                from .session_closure import parse_digest
                expected = {'activeContext.md': parse_digest(args.expected_active),
                            'decisions.md': parse_digest(args.expected_decisions)}
                result = hub.handoff(args.request, attempt=args.attempt, outcome=args.outcome, detail=args.detail,
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
        if verb == 'event':
            print('{}')
            return 0
        print('asha control session: ' + str(exc), file=sys.stderr)
        return 2
