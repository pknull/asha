"""A session dashboard. Workflows live inside harnesses or the advanced view.

The pure parts live beside it: ``session_view`` (the retained model),
``session_layout`` (regions and renders), ``session_keys`` (footer and key
sheet) and ``session_actions`` (row actions). This module owns curses.
"""
from __future__ import annotations
try:
    import curses
except ImportError:
    curses = None
import locale
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from .config import load_config
from .hub_cli import overview
from .session_hub import Hub, listed, no_handoff_enabled
from . import session_actions, session_layout, session_preview, session_title, session_view
from .session_actions import launch_selection  # noqa: F401  (re-exported; #95 tests)
from .session_keys import footer, key_sheet, sheet_lines as _sheet_lines, sheet_offset  # noqa: F401
from .session_presentation import present  # noqa: F401  (re-exported for callers and tests)

QUIT = object()
ESC, SPACE = 27, ord(' ')
REFRESH_SECONDS = 2


def _key(name, default):
    return getattr(curses, name, default)


def lines(snapshot, *, selected=0, width=100, height=30, message='', anchor=None, keys=False, sheet=0,
          peek=False, preview=True):
    """The dashboard as plain text lines, exactly as painted."""
    return session_layout.plain(session_layout.render(
        snapshot, selected=selected, anchor=anchor, width=width, height=height, message=message, keys=keys,
        sheet=sheet, peek=peek, preview=preview))


def _span_attribute(role, tier, selected, coloured):
    from .tui import _attribute
    attr = _attribute(curses, tier, coloured)
    if role in {'heading', 'section'}:
        attr |= curses.A_BOLD
        return attr | (curses.A_REVERSE if selected else 0)
    if role == 'row':
        return curses.A_REVERSE if selected else 0
    # A status span keeps its foreground even on the reverse-video selection.
    return attr


def _paint(screen, snapshot, *, selected=0, coloured=False, message='', anchor=None, keys=False, sheet=0,
           peek=False, preview=True):
    height, width = screen.getmaxyx()
    limit = max(0, width - 1)
    screen.erase()
    rendered = session_layout.render(snapshot, selected=selected, anchor=anchor, width=limit, height=height,
                                     message=message, keys=keys, sheet=sheet, peek=peek, preview=preview)
    for y, spans in enumerate(rendered):
        for x, text, role, tier, chosen in spans:
            # Spans are clipped in cells; addnstr counts characters, which is never more.
            try:
                screen.addnstr(y, x, text, max(0, limit - x), _span_attribute(role, tier, chosen, coloured))
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


def _unicode_ok():
    return (locale.getpreferredencoding(False) or '').lower().replace('-', '') == 'utf8'


def _tmux_reader(env):
    """Read one tmux value for the title policy; None when tmux cannot answer."""
    def read(argv):
        try:
            done = subprocess.run(['tmux', *argv], capture_output=True, text=True, timeout=1, env=env)
        except (OSError, subprocess.SubprocessError):
            return None
        return done.stdout.strip() if done.returncode == 0 else None
    return read


def _tigetstr(name):
    try:
        return curses.tigetstr(name)
    except (curses.error, AttributeError):
        return None


def _title_writer(env):
    stream = getattr(sys.stdout, 'buffer', None)
    return session_title.open_writer(env, stream=stream, tmux=_tmux_reader(env), tigetstr=_tigetstr) \
        if stream is not None else session_title.TitleWriter(None, enabled=False)


def _loop(screen, config, env):
    return Dashboard(screen, config, env).run()


class Dashboard:
    """The event loop's state: the retained view, the page in flight and the display toggles."""

    def __init__(self, screen, config, env):
        from . import tui
        screen.timeout(200)
        self.screen, self.config, self.env = screen, config, env
        self.model = tui.TuiModel([])
        self.model.coloured = tui.init_colours(curses)
        self.hub = Hub(config, env=env)
        self.ctx = session_actions.Context(screen, curses, self.model, config, env, self.hub)
        # The view is retained across refreshes (#102): rows keep their stable
        # order, selection is an identity and an incomplete page marks rows stale.
        self.view = session_view.ViewModel()
        self.page = {'summary': 'Reading sessions…', 'errors': []}
        # ``sheet`` is the key sheet's scroll offset while it is shown, else None.
        self.next_refresh, self.message, self.sheet, self.started = 0.0, '', None, 0.0
        self.include_closed, self.peek, self.preview = False, False, True
        self.ascii = not _unicode_ok()
        self.title = _title_writer(env)
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='asha-session-view')
        self.future = None
        self.fitted = None   # (view, list height) the last automatic fold was computed for
        # The read-only preview (#102 phase 3) reads on its own worker thread, and
        # only when control.session_preview opts in; off, nothing is ever read.
        self.previews = session_preview.Poller(reader=session_preview.reader_for(config, self.hub.tmux)) \
            if session_preview.enabled(config) else None

    def run(self):
        try:
            while True:
                self.poll()
                self.paint()
                if self.handle(self.screen.getch()) is QUIT:
                    return 0
        finally:
            self.pool.shutdown(wait=False, cancel_futures=True)
            if self.previews is not None:
                self.previews.close()
            self.title.close()

    def poll(self):
        if self.future is None and time.monotonic() >= self.next_refresh:
            self.started = time.time()
            self.future = self.pool.submit(overview, self.config, env=self.env, include_closed=self.include_closed)
        if self.future is not None and self.future.done():
            try:
                self.page = self.future.result()
                self.view = session_view.merge(self.view, self.page['rows'], observed_at=self.started,
                                               complete=bool(self.page.get('complete')))
            except Exception as exc:
                self.message = 'Status unavailable: ' + str(exc)
                self.view = session_view.merge(self.view, [], observed_at=self.started, complete=False)
            self.future, self.next_refresh = None, time.monotonic() + REFRESH_SECONDS
        box = self.box()
        if self.previews is not None and box.mode in ('wide', 'peek') and self.sheet is None:
            # Only the selected row, and only while its preview is on screen.
            self.previews.tick(self.previewed(), lines=max(1, box.list_height))

    def previewed(self):
        row = self.selected_row()
        return row if row and row.get('kind') != 'section' else None

    def display(self):
        summary = self.page.get('summary', 'Reading sessions…')
        summary += ' (input filter)' if self.view.input_only else ''
        summary += f'; {len(self.view.stale)} stale' if self.view.stale else ''
        shown = {'preview': self.previews.current(self.previewed())} \
            if self.previews is not None and self.box().mode in ('wide', 'peek') else {}
        return {**shown, 'session_preview': self.previews is not None, 'rows': session_view.display_rows(self.view), 'summary': summary,
                'errors': self.page.get('errors', []), 'no_handoff_close': no_handoff_enabled(self.config),
                'grouping': self.view.grouping, 'ascii': self.ascii, 'now': time.time(),
                'attention': session_view.attention_counts(self.view)}

    def paint(self):
        box = self.box()
        if box.mode in ('narrow', 'wide') and (self.fitted is None or self.fitted[0] is not self.view
                                               or self.fitted[1] != box.list_height):
            # §5.6: fold Ended/History, then finished rows, until the list fits (Q14-F3).
            self.view = session_view.fit(self.view, box.list_height)
            self.fitted = (self.view, box.list_height)
        _paint(self.screen, self.display(), selected=session_view.selected_index(self.view),
               coloured=self.model.coloured, message=self.message, anchor=self.view.anchor,
               keys=self.sheet is not None, sheet=self.sheet or 0, peek=self.peek, preview=self.preview)
        self.title.set(session_title.title_text(session_view.attention_counts(self.view)))

    def box(self):
        height, width = self.screen.getmaxyx()
        return session_layout.layout(height, max(0, width - 1), errors=bool(self.page.get('errors')),
                                     peek=self.peek, preview=self.preview)

    def capacity(self):
        return max(1, self.box().list_height)

    def refresh_row(self, sid, transport):
        """Re-read only the acted-on row; the regular tick observes the rest."""
        try:
            if transport == 'terminal' or self.hub.owns(sid):
                shown = self.hub.show(sid)
                # The active query decides membership; a close while history is
                # off removes the row rather than painting it as history.
                self.view = session_view.merge_row(self.view, shown, observed_at=time.time(),
                                                   member=listed(shown, include_closed=self.include_closed))
                return
        except (ValueError, OSError, KeyError):
            pass
        # Legacy Rooms and unowned structured sessions have no single-row read.
        self.next_refresh = 0

    def sheet_key(self, key):
        height = self.screen.getmaxyx()[0]
        enabled = no_handoff_enabled(self.config)
        if key in (curses.KEY_DOWN, curses.KEY_UP):
            self.sheet = sheet_offset(self.sheet + (1 if key == curses.KEY_DOWN else -1), height=height,
                                      no_handoff_close=enabled)
        elif key == _key('KEY_RESIZE', 410):
            # Q13-F1: a resize keeps the sheet open at its place, clamped to the new height.
            self.sheet = sheet_offset(self.sheet, height=height, no_handoff_close=enabled)
        elif key != -1:
            self.sheet = None

    def selected_row(self):
        return session_view.display_rows(self.view)[session_view.selected_index(self.view)] \
            if self.view.order else None

    def toggle_preview(self):
        if self.screen.getmaxyx()[1] - 1 >= session_layout.WIDE:
            self.preview = not self.preview
        else:
            self.peek = not self.peek

    def view_key(self, key, row):
        """Keys that only change what is shown; True when handled."""
        view = self.view
        if key == curses.KEY_DOWN:
            view = session_view.move(view, 1, visible=self.capacity())
        elif key == curses.KEY_UP:
            view = session_view.move(view, -1, visible=self.capacity())
        elif key == ord('g'):
            view = session_view.with_changes(view, grouping='state' if view.grouping == 'project' else 'project')
            self.message = 'Grouped by ' + view.grouping
        elif key == ord('!'):
            view = session_view.jump_attention(view)
            self.message = '' if view is not self.view else 'No session needs you'
        elif key == _key('KEY_LEFT', 260):
            view = session_view.fold(view, fold=True)
        elif key == _key('KEY_RIGHT', 261) or (row and row.get('kind') == 'section'
                                               and key in (*session_actions.ENTER, _key('KEY_ENTER', 343))):
            view = session_view.fold(view, fold=False) if row and row.get('kind') == 'section' else view
        elif key == SPACE:
            self.toggle_preview()
        elif key == ESC:
            self.peek = False
        elif key == ord('M'):
            view = session_view.with_changes(view, input_only=not view.input_only)
            self.message = 'Showing input requests' if view.input_only else 'Showing all sessions'
        elif key == ord('A'):
            # A different query, so this is the one key that re-reads the page.
            self.include_closed = not self.include_closed
            self.next_refresh = 0
            self.message = 'Including retained history' if self.include_closed else 'Showing current sessions'
        else:
            return False
        self.view = view
        return True

    def suspend_for_workflows(self):
        from . import tui
        curses.def_prog_mode()
        curses.endwin()
        try:
            tui.run_tui(self.env, initial_mode='initiatives')
        finally:
            curses.reset_prog_mode()
            self.model.coloured = tui.init_colours(curses)
            self.screen.timeout(200)
            tui._repaint_after_suspend(self.screen)
            self.next_refresh = 0

    def handle(self, key):
        if self.sheet is not None:
            self.sheet_key(key)
            return None
        if key in (-1, _key('KEY_RESIZE', 410)):
            return None
        if key == ord('q'):
            return QUIT
        if key == ord('?'):
            self.sheet = 0
            return None
        row = self.selected_row()
        if self.view_key(key, row):
            return None
        session = row if row and row.get('kind') != 'section' else None
        enter = key in session_actions.ENTER or key == _key('KEY_ENTER', 343)
        acted = session if session and (enter or key in session_actions.ACTION_KEYS) else None
        try:
            if key in (ord('n'), ord('o')):
                message, sid = session_actions.launch(self.ctx, key)
                self.message = message or ''
                if sid:
                    self.refresh_row(sid, 'terminal')
            elif key == ord('G'):
                self.suspend_for_workflows()
            elif acted:
                result = session_actions.act(self.ctx, key, acted)
                if result is not None:
                    self.message = result
        except (ValueError, OSError) as exc:
            self.message = str(exc)
        if acted:
            self.refresh_row(acted['session_id'], acted.get('transport'))
        return None
