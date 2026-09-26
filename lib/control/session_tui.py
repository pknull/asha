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
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from .config import load_config
from .hub_cli import overview
from .session_hub import Hub, listed, no_handoff_enabled
from . import session_view
from .session_presentation import memory_label, present, receipt_label
from .tmux import TmuxAdapter
from .tui_style import BAD, GOOD, INERT, MACHINE, WAITING, tier_for
from .session_selection import label as selection_label



def launch_selection(model_text, effort_text):
    """Optional launch fields (#95): a blank answer means the harness default."""
    return {'model': (model_text or '').strip() or None, 'effort': (effort_text or '').strip() or None}

def _activity_tier(activity):
    # Session activity names differ from the advanced workflow state names.
    return {
        'working': MACHINE, 'queued': MACHINE, 'waiting-input': WAITING,
        'finished': GOOD, 'blocked': BAD, 'uncertain': BAD, 'budget-exhausted': BAD,
    }.get(activity, tier_for(activity))


# Footer keys by priority; lower-priority keys drop first on a narrow terminal.
_TAIL = '? keys  q quit'


def footer(row, *, width, no_handoff_close=False):
    """One state-aware line: the keys that matter for the selected row (#102)."""
    if row is None:
        keys = ['n job', 'o Room', 'A history', 'G workflows']
    else:
        step, group = row.get('next_step', ''), row.get('group', 'current')
        answer = ['a answer'] if row.get('activity') in {'needs-input', 'permission-requested', 'waiting-input'} \
            and row.get('transport') == 'structured' else []
        if group == 'history':
            keys = ['r resume', 'Enter view', 'A history']
        elif step.startswith('Close failed'):
            keys = ['x retry close', 'X force-close', 'Enter attach']
        elif group == 'ended':
            keys = ['x close', 'r resume', 'X force-close', 'Enter attach']
        else:
            keys = answer + ['Enter attach', 'm send', 'x close', 's stop']
        if no_handoff_close and (row.get('no_handoff') or {}).get('eligible'):
            keys.insert(1, 'c close (no handoff)')
    from .tui import _cell_width
    while keys and _cell_width('  '.join(keys + [_TAIL])) > width:
        keys.pop()
    return '  '.join(keys + [_TAIL])


def key_sheet(*, no_handoff_close=False):
    """Every binding, labelled as the footer labels it."""
    entries = [('Up/Down', 'select a session'),
               ('Enter attach', 'open the terminal or structured conversation'),
               ('a answer', 'answer the pending input request'),
               ('m send', 'queue a message for the session'),
               ('x close', 'close, requesting a memory handoff'),
               *([('c close (no handoff)', 'close at a verified native idle; no save claimed')]
                 if no_handoff_close else []),
               ('X force-close', 'close without a new handoff'),
               ('s stop', 'stop the session; history is retained'),
               ('r resume', 'resume with a continuation'),
               ('n job', 'start a project job'), ('o Room', 'open a project Room'),
               ('M input filter', 'show only sessions that need input'),
               ('A history', 'include retained history'),
               ('G workflows', 'advanced initiatives view'),
               ('? keys', 'this sheet'), ('q quit', 'leave; sessions keep running')]
    return ['Keys (any key returns)'] + [f'  {key:<22}{text}' for key, text in entries]


def _sheet_page(height):
    """Entries per page when the sheet needs a heading and a position line."""
    return max(1, height - 2)


def sheet_offset(offset, *, height, no_handoff_close=False):
    """Clamp a key-sheet scroll offset for this height; 0 when the sheet fits."""
    entries = len(key_sheet(no_handoff_close=no_handoff_close)) - 1
    if entries + 1 <= height:
        return 0
    return max(0, min(offset, entries - _sheet_page(height)))


def _sheet_lines(height, offset, *, no_handoff_close):
    """The key sheet, paged on a short terminal so every binding stays reachable."""
    sheet = key_sheet(no_handoff_close=no_handoff_close)
    if len(sheet) <= height:
        return sheet
    entries = sheet[1:]
    first = sheet_offset(offset, height=height, no_handoff_close=no_handoff_close)
    page = entries[first:first + _sheet_page(height)]
    return (['Keys (Up/Down scroll; other keys return)', *page,
             f'  {first + 1}-{first + len(page)} of {len(entries)} · Up/Down for more'])


def _utc(stamp, pattern):
    return datetime.fromtimestamp(stamp, timezone.utc).strftime(pattern)


def _row_marks(row):
    """Receipt (#101) and staleness facts shown on the session row itself."""
    marks = []
    receipt = (row.get('completion_readiness') or {}).get('receipt')
    if receipt in {'current', 'stale'}:
        marks.append(receipt_label(row))
    if row.get('stale_since') is not None:
        marks.append('stale since ' + _utc(row['stale_since'], '%H:%M:%S UTC'))
    return ''.join('  ' + mark for mark in marks)


def _render_lines(snapshot, *, selected=0, width=100, height=30, message='', anchor=None, keys=False, sheet=0):
    from .tui import _clip
    if height < 8:
        return [(_clip(line, width), 'heading' if i == 0 else 'muted', None)
                for i, line in enumerate(['ASHA CONTROL', 'Enlarge terminal', 'q quit'][:height])]
    rows = [row if 'next_step' in row and 'group' in row else present(row) for row in snapshot.get('rows', [])]
    enabled = bool(snapshot.get('no_handoff_close'))
    if keys:
        sheet = [(line, 'heading' if i == 0 else 'muted', None if i == 0 else INERT)
                 for i, line in enumerate(_sheet_lines(height, sheet, no_handoff_close=enabled))]
        return [(_clip(line, width), role, tier) for line, role, tier in sheet[:height]]
    result = [('ASHA CONTROL — Sessions', 'heading', None),
              (snapshot.get('summary', 'Reading sessions…'), 'summary',
               WAITING if any(row['activity'] in {'needs-input', 'waiting-input'} for row in rows) else None),
              ('   NEXT STEP                       PROJECT / SESSION                 HARNESS', 'heading', None)]
    selected = min(selected, len(rows) - 1) if rows else 0
    space = max(0, height - 8 - bool(snapshot.get('errors')))
    # Hold the selected row's screen line across refreshes (#102); headings
    # are lines too, so one appearing above the row scrolls rather than pushes.
    start = session_view.viewport_start(rows, selected, space - 1 if anchor is None else anchor, space) \
        if rows else 0
    previous_group = None
    for i, row in enumerate(rows[start:], start):
        heading = row['group'] != previous_group and row['group'] != 'current'
        needed = 1 + bool(heading)
        if space < needed:
            break
        if heading:
            result.append(('Ended sessions' if row['group'] == 'ended' else 'Retained history', 'heading', INERT))
        previous_group = row['group']
        space -= needed
        chosen = selection_label(row, compact=True)
        result.append((f"{'>' if i == selected else ' '} {row['next_step']:<32} {row['project_name']} / {row['name']}"
                       f"  [{row['harness']}{' ' + chosen if chosen else ''}]{_row_marks(row)}",
                       'selected' if i == selected else 'row', _activity_tier(row['activity'])))
    current = rows[selected] if rows else None
    if current:
        row = current
        capture = (row.get('closure') or {}).get('capture') or row.get('capture') or {}
        experience = (f" · capture:{capture.get('status', 'disabled')}"
                      f" review:{row.get('experience_review', 'none')}") if capture else ''
        saved = memory_label(row)
        detail = (saved + ' · ' if saved else '') + row.get('reason', '')
        chosen = selection_label(row)
        receipt = receipt_label(row)
        result += [('', 'muted', INERT), (detail, 'detail', _activity_tier(row['activity'])),
                   (f"{row['session_id']} · {(receipt + ' · ') if receipt else ''}"
                    f"{row.get('pending_messages', 0)} queued messages" + experience
                    + (' · ' + chosen if chosen else ''), 'muted', INERT)]
    result += [(error, 'error', BAD) for error in snapshot.get('errors', [])[:1]]
    result += [(message, 'message', None)]
    result = result[:height - 1] + [(footer(current, width=width, no_handoff_close=enabled), 'muted', INERT)]
    return [(_clip(str(line), width), role, tier) for line, role, tier in result]


def lines(snapshot, *, selected=0, width=100, height=30, message='', anchor=None, keys=False, sheet=0):
    return [line for line, _, _ in _render_lines(
        snapshot, selected=selected, width=width, height=height, message=message, anchor=anchor, keys=keys,
        sheet=sheet)]


def _paint(screen, snapshot, *, selected=0, coloured=False, message='', anchor=None, keys=False, sheet=0):
    from .tui import _attribute, _cell_width, _prefix_cells
    height, width = screen.getmaxyx()
    limit = max(0, width - 1)
    screen.erase()
    for y, (line, role, tier) in enumerate(_render_lines(
            snapshot, selected=selected, width=limit, height=height, message=message, anchor=anchor, keys=keys,
            sheet=sheet)):
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
                stop = min(len(line), 34)
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
    # The view is retained across refreshes (#102): rows keep their stable
    # order, selection is an identity and an incomplete page marks rows stale.
    view = session_view.ViewModel()
    page = {'summary': 'Reading sessions…', 'errors': []}
    # ``sheet`` is the key sheet's scroll offset while it is shown, else None.
    next_refresh, message, sheet, started = 0.0, '', None, 0.0
    include_closed = False
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='asha-session-view')
    future = None

    def display():
        summary = page.get('summary', 'Reading sessions…')
        summary += ' (input filter)' if view.input_only else ''
        summary += f'; {len(view.stale)} stale' if view.stale else ''
        return {'rows': session_view.display_rows(view), 'summary': summary,
                'errors': page.get('errors', []), 'no_handoff_close': no_handoff_enabled(config)}

    def capacity():
        return max(1, screen.getmaxyx()[0] - 10)

    def refresh_row(sid, transport):
        """Re-read only the acted-on row; the regular tick observes the rest."""
        nonlocal view, next_refresh
        try:
            if transport == 'terminal' or hub.owns(sid):
                shown = hub.show(sid)
                # The active query decides membership; a close while history is
                # off removes the row rather than painting it as history.
                view = session_view.merge_row(view, shown, observed_at=time.time(),
                                              member=listed(shown, include_closed=include_closed))
                return
        except (ValueError, OSError, KeyError):
            pass
        # Legacy Rooms and unowned structured sessions have no single-row read.
        next_refresh = 0

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
                started = time.time()
                future = pool.submit(overview, config, env=env, include_closed=include_closed)
            if future is not None and future.done():
                try:
                    page = future.result()
                    view = session_view.merge(view, page['rows'], observed_at=started,
                                              complete=bool(page.get('complete')))
                except Exception as exc:
                    message = 'Status unavailable: ' + str(exc)
                    view = session_view.merge(view, [], observed_at=started, complete=False)
                future, next_refresh = None, time.monotonic() + 2
            _paint(screen, display(), selected=session_view.selected_index(view), coloured=model.coloured,
                   message=message, anchor=view.anchor, keys=sheet is not None, sheet=sheet or 0)
            key = screen.getch()
            if sheet is not None:
                if key in (curses.KEY_DOWN, curses.KEY_UP):
                    sheet = sheet_offset(sheet + (1 if key == curses.KEY_DOWN else -1),
                                         height=screen.getmaxyx()[0], no_handoff_close=no_handoff_enabled(config))
                elif key != -1:
                    sheet = None
                continue
            row = session_view.display_rows(view)[session_view.selected_index(view)] if view.order else None
            if key == ord('q'):
                return 0
            if key == ord('?'):
                sheet = 0
            elif key == curses.KEY_DOWN:
                view = session_view.move(view, 1, visible=capacity())
            elif key == curses.KEY_UP:
                view = session_view.move(view, -1, visible=capacity())
            acted = row if row and key in (10, 13, curses.KEY_ENTER, ord('a'), ord('m'), ord('x'), ord('c'),
                                           ord('X'), ord('s'), ord('r')) else None
            try:
                if key in (ord('n'), ord('o')):
                    project = prompt('Project: ', 'Launch project session')
                    assignment = prompt('Assignment: ' if key == ord('n') else 'Topic: ', 'New session') if project else None
                    if assignment:
                        harness = prompt('Harness [claude]: ', 'claude / codex / copilot / opencode')
                        chosen = None
                        if harness is not None:
                            model_text = prompt('Model [default]: ', 'Optional native model; blank keeps the harness default')
                            effort_text = prompt('Effort [default]: ', 'Optional reasoning effort; blank keeps the harness default') \
                                if model_text is not None else None
                            chosen = launch_selection(model_text, effort_text) if effort_text is not None else None
                        if chosen is not None:
                            created = hub.launch(project=project, prompt=assignment, harness=harness.strip() or 'claude',
                                                 profile='room' if key == ord('o') else 'worker', **chosen)
                            message = 'Started ' + created['name']
                            refresh_row(created['session_id'], 'terminal')
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
                        next_refresh = 0
                elif key == ord('M'):
                    view = session_view.with_changes(view, input_only=not view.input_only)
                    message = 'Showing input requests' if view.input_only else 'Showing all sessions'
                elif key == ord('A'):
                    # A different query, so this is the one key that re-reads the page.
                    include_closed = not include_closed
                    next_refresh = 0
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
                elif row and key == ord('c') and not no_handoff_enabled(config):
                    message = 'Close at idle (no handoff) is off: control.no_handoff_close, #103; x requests a handoff'
                elif row and key in (ord('x'), ord('c'), ord('X'), ord('s')):
                    label = {ord('x'): 'Close (request handoff) ', ord('c'): 'Close at idle (no handoff, no save claimed) ',
                             ord('X'): 'Force-close (no new handoff) ', ord('s'): 'Stop '}[key]
                    if prompt('Type yes: ', label + row['name']) == 'yes':
                        refuse_managed_operator(config, env)
                        if hub.owns(row['session_id']):
                            if key in (ord('x'), ord('c')):
                                closed = hub.close(row['session_id'], no_handoff=key == ord('c'))
                                message = (closed.get('closure') or {}).get('guidance') or 'Session closed'
                            else:
                                stopped = hub.stop(row['session_id'], close=key == ord('X'))
                                message = 'Session stopped; history retained; ' + (memory_label(stopped) or 'no memory handoff claimed')
                        elif key in (ord('x'), ord('c')):
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
            except (ValueError, OSError) as exc:
                message = str(exc)
            if acted:
                refresh_row(acted['session_id'], acted.get('transport'))
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
