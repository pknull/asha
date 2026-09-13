"""A session dashboard. Workflows live inside harnesses or the advanced view."""
from __future__ import annotations
try:
    import curses
except ImportError:
    curses = None
import signal
import sys
import time
import uuid
import textwrap
from concurrent.futures import ThreadPoolExecutor

from .config import load_config
from .hub_cli import overview
from .session_hub import Hub
from .tmux import TmuxAdapter
from .tui_style import BAD, GOOD, INERT, MACHINE, WAITING, tier_for


def _activity_tier(activity):
    # Session activity names differ from the advanced workflow state names.
    return {
        'working': MACHINE, 'queued': MACHINE, 'waiting-input': WAITING,
        'finished': GOOD, 'blocked': BAD, 'uncertain': BAD, 'budget-exhausted': BAD,
    }.get(activity, tier_for(activity))


def _render_lines(snapshot, *, selected=0, width=100, height=30, message=''):
    from .tui import _clip
    rows = snapshot.get('rows', [])
    result = [('ASHA CONTROL — Sessions', 'heading', None),
              (snapshot.get('summary', 'Reading sessions…'), 'summary',
               WAITING if any(row['activity'] in {'needs-input', 'waiting-input'} for row in rows) else None),
              ('   STATUS        PROJECT / SESSION                         HARNESS', 'heading', None)]
    help_lines = textwrap.wrap('Enter attach | a input | m send | n job | o Room | x close (handoff) | X force-close | s stop | r resume | M input list | A history | G workflows | q quit', width=max(1, width))
    space = max(0, height - 7 - bool(snapshot.get('errors')) - len(help_lines))
    start = max(0, selected - space + 1)
    for i, row in enumerate(rows[start:start + space], start):
        result.append((f"{'>' if i == selected else ' '} {row['activity']:<13} {row['project_name']} / {row['name']}  [{row['harness']}]",
                       'selected' if i == selected else 'row', _activity_tier(row['activity'])))
    if rows:
        row = rows[min(selected, len(rows) - 1)]
        capture = (row.get('closure') or {}).get('capture') or row.get('capture') or {}
        experience = (f" · capture:{capture.get('status', 'disabled')}"
                      f" review:{row.get('experience_review', 'none')}") if capture else ''
        result += [('', 'muted', INERT), (row.get('reason', ''), 'detail', _activity_tier(row['activity'])),
                   (f"{row['session_id']} · {row.get('pending_messages', 0)} queued messages" + experience, 'muted', INERT)]
    result += [(error, 'error', BAD) for error in snapshot.get('errors', [])[:1]]
    result += [(message, 'message', None)]
    if height < 8:
        return [(_clip(line, width), 'heading' if i == 0 else 'muted', None)
                for i, line in enumerate(['ASHA CONTROL', 'Enlarge terminal', 'q quit'][:height])]
    result = result[:max(0, height - len(help_lines))] + [(line, 'muted', INERT) for line in help_lines]
    return [(_clip(str(line), width), role, tier) for line, role, tier in result[:height]]


def lines(snapshot, *, selected=0, width=100, height=30, message=''):
    return [line for line, _, _ in _render_lines(
        snapshot, selected=selected, width=width, height=height, message=message)]


def _paint(screen, snapshot, *, selected=0, coloured=False, message=''):
    from .tui import _attribute, _cell_width, _prefix_cells
    height, width = screen.getmaxyx()
    limit = max(0, width - 1)
    screen.erase()
    for y, (line, role, tier) in enumerate(_render_lines(
            snapshot, selected=selected, width=limit, height=height, message=message)):
        # addnstr limits characters, not terminal cells. Keep wide names in bounds.
        if _cell_width(line) > limit:
            line = _prefix_cells(line, max(0, limit - 1)) + ('…' if limit else '')
        attr = _attribute(curses, tier, coloured)
        if role == 'heading':
            attr |= curses.A_BOLD
        elif role in {'row', 'selected'}:
            attr = curses.A_REVERSE if role == 'selected' else 0
        try:
            screen.addnstr(y, 0, line, limit, attr)
            if role in {'row', 'selected'} and limit > 2:
                # Keep the status foreground even on the reverse-video selection.
                # The fixed prefix/status are ASCII; project names may be wide.
                stop = min(len(line), 2 + max(13, len(line[2:].split(' ', 1)[0])))
                screen.addnstr(y, 2, line[2:stop], stop - 2,
                               _attribute(curses, tier, coloured))
        except curses.error:
            pass
    screen.refresh()


def run_tui(env):
    from .tui import _TuiShutdown
    def fallback():
        print('asha control: a usable terminal is required; use `asha control session list --json`.', file=sys.stderr)
        return 2
    if curses is None or not sys.stdin.isatty() or not sys.stdout.isatty():
        return fallback()
    try:
        curses.setupterm()
    except (curses.error, OSError):
        return fallback()
    config = load_config(env)
    previous = {}
    def shutdown(signum, frame):
        raise _TuiShutdown(signum)
    try:
        for signum in (signal.SIGTERM, signal.SIGHUP):
            previous[signum] = signal.signal(signum, shutdown)
        try:
            return curses.wrapper(_loop, config, dict(env))
        except _TuiShutdown as exc:
            return 128 + exc.signum
        except curses.error:
            return fallback()
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def _loop(screen, config, env):
    from . import tui
    from .session_store import SessionStore
    from .sessions import refuse_managed_operator
    from .rooms import RoomStore, attach_room, close_room
    screen.timeout(200)
    model = tui.TuiModel([])
    model.coloured = tui.init_colours(curses)
    hub = Hub(config, env=env)
    snapshot = {'rows': []}
    selected, next_refresh, message = 0, 0.0, ''
    include_closed, input_only = False, False
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='asha-session-view')
    future = None

    def prompt(label, title):
        h, w = screen.getmaxyx()
        model.resize(h, w)
        return tui._prompt_line(screen, curses, model, label, title=title, maximum=4000)

    def attach(row):
        if row['transport'] == 'structured':
            return tui._managed_session_view(screen, curses, config, row['session_id'])
        target = hub.attach(row['session_id']) if row['transport'] == 'terminal' else attach_room(
            RoomStore(config), row['room_id'], tmux=hub.tmux)
        return tui._popup_room_command(screen, curses, config, env, hub.tmux,
                                       target['attach_argv'], target['attach'], target['name']) or 'Session detached; work continues'

    try:
        while True:
            if future is None and time.monotonic() >= next_refresh:
                future = pool.submit(overview, config, env=env, include_closed=include_closed)
            if future is not None and future.done():
                current_id = snapshot['rows'][selected]['session_id'] if snapshot['rows'] else None
                try:
                    snapshot = future.result()
                    if input_only:
                        snapshot['rows'] = [r for r in snapshot['rows'] if r['activity'] == 'needs-input']
                        snapshot['summary'] += ' (input filter)'
                    selected = next((i for i, r in enumerate(snapshot['rows']) if r['session_id'] == current_id), 0)
                except Exception as exc:
                    message = 'Status unavailable: ' + str(exc)
                future, next_refresh = None, time.monotonic() + 2
            _paint(screen, snapshot, selected=selected, coloured=model.coloured, message=message)
            key = screen.getch()
            rows = snapshot['rows']
            row = rows[selected] if rows else None
            if key == ord('q'):
                return 0
            if key == curses.KEY_DOWN and rows:
                selected = min(len(rows) - 1, selected + 1)
            elif key == curses.KEY_UP:
                selected = max(0, selected - 1)
            try:
                if key in (ord('n'), ord('o')):
                    project = prompt('Project: ', 'Launch project session')
                    assignment = prompt('Assignment: ' if key == ord('n') else 'Topic: ', 'New session') if project else None
                    if assignment:
                        harness = prompt('Harness [claude]: ', 'claude / codex / copilot / opencode')
                        if harness is not None:
                            created = hub.launch(project=project, prompt=assignment, harness=harness.strip() or 'claude', profile='room' if key == ord('o') else 'worker')
                            message = 'Started ' + created['name']
                elif key == ord('G'):
                    curses.def_prog_mode()
                    curses.endwin()
                    try:
                        tui.run_tui(env, initial_mode='initiatives')
                    finally:
                        curses.reset_prog_mode()
                        model.coloured = tui.init_colours(curses)
                        screen.timeout(200)
                        tui._repaint_after_suspend(screen)
                elif key == ord('M'):
                    input_only = not input_only
                    message = 'Showing input requests' if input_only else 'Showing all sessions'
                elif key == ord('A'):
                    include_closed = not include_closed
                    message = 'Including retained history' if include_closed else 'Showing current sessions'
                elif row and key in (10, 13, curses.KEY_ENTER):
                    message = attach(row) or ''
                elif row and key == ord('a'):
                    if row['transport'] != 'structured':
                        message = attach(row) or ''
                    else:
                        with SessionStore(config) as sessions:
                            requests = sessions.snapshot(row['session_id'])['requests']
                        request = next((r for r in requests if r['state'] == 'pending'), None)
                        if request and request['kind'] == 'native-permission':
                            message = tui._decide_managed_permission(screen, curses, model, config, env, request['request_id'])
                        elif request:
                            tui._execute_intent(tui.TuiIntent(tui.IntentKind.SESSION_QUESTIONS, task_id=request['request_id']), stdscr=screen,
                                                curses_module=curses, model=model, config=config, env=env,
                                                store=None, journals=None, jj=None)
                            message = model.message or ''
                        else:
                            message = 'No input request is recorded; Enter opens the conversation'
                elif row and key == ord('m'):
                    body = prompt('Message: ', 'Send context to ' + row['name'])
                    if body:
                        refuse_managed_operator(config, env)
                        if row['transport'] == 'terminal':
                            hub.send(row['session_id'], body, key=str(uuid.uuid4()))
                            message = 'Queued; attach to the session or let its worker read messages. Not delivered yet.'
                        elif row['transport'] == 'structured':
                            if hub.owns(row['session_id']):
                                hub.send(row['session_id'], body, key=str(uuid.uuid4()))
                            else:
                                with SessionStore(config) as sessions:
                                    sessions.enqueue(row['session_id'], body, key=str(uuid.uuid4()))
                            message = 'Message retained for the next eligible turn'
                        else:
                            message = 'Enter attaches to this legacy Room to provide input directly'
                elif row and key in (ord('x'), ord('X'), ord('s')):
                    label = {ord('x'): 'Close (request handoff) ', ord('X'): 'Force-close (no memory save) ', ord('s'): 'Stop '}[key]
                    if prompt('Type yes: ', label + row['name']) == 'yes':
                        refuse_managed_operator(config, env)
                        if hub.owns(row['session_id']):
                            if key == ord('x'):
                                closed = hub.close(row['session_id'])
                                message = (closed.get('closure') or {}).get('guidance') or 'Session closed'
                            else:
                                hub.stop(row['session_id'], close=key == ord('X'))
                                message = 'Session stopped; history retained; no memory handoff claimed'
                        elif key == ord('x'):
                            message = 'No handoff seam for a legacy Room or managed session; X force-closes, s stops'
                        elif row['transport'] == 'room':
                            close_room(RoomStore(config), row['room_id'], tmux=hub.tmux)
                            message = 'Room closed; history retained; no memory handoff claimed'
                        else:
                            with SessionStore(config) as sessions:
                                sessions.stop(row['session_id'])
                            message = 'Session stopped; history retained; no memory handoff claimed'
                elif row and key == ord('r'):
                    body = prompt('Continuation: ', 'Resume ' + row['name'])
                    if body:
                        if row['transport'] == 'terminal':
                            hub.resume(row['session_id'], prompt=body)
                            message = 'Session resumed'
                        elif row['transport'] == 'structured':
                            refuse_managed_operator(config, env)
                            with SessionStore(config) as sessions:
                                state = sessions.get(row['session_id'])
                                digest = sessions.recovery_digest(state)
                            detail = state.get('recovery') or state['state']
                            if prompt('Type yes: ', 'Resume with new context; retained state: ' + str(detail)) == 'yes':
                                if hub.owns(row['session_id']):
                                    hub.resume(row['session_id'], prompt=body, expected_digest=digest)
                                else:
                                    with SessionStore(config) as sessions:
                                        sessions.resume(row['session_id'], prompt=body, expected_digest=digest)
                                message = 'Continuation queued; prior uncertain input will not be replayed'
                        else:
                            message = 'Open a new Room with continuation context'
                if key != -1:
                    next_refresh = 0
            except (ValueError, OSError) as exc:
                message = str(exc)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
