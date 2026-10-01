"""The dashboard's read-only live preview (#102 phase 3).

Sources by transport (design §4.1): a terminal or Room session shows its pane
through ``pane_peek`` after a Room ownership check; a structured session shows
its retained text/tool events, read without naming a consumer so no
delivery cursor ever moves; an ended session shows no screen at all.

``Poller`` reads only the selected row on its own daemon worker thread and
drops a result whose selection is no longer current. A read starts no sooner
than ``INTERVAL`` seconds after the previous one finished, so the tmux capture
commands themselves, not just the submissions, are at least that far apart.
Closing it stops scheduling and cancels a read still running: a queued read
never starts, a pane read issues no further tmux command and a structured read
stops before its next page. A tmux call already running ends at its own
deadline, unseen, and never holds the dashboard's exit.
The preview is off unless ``control.session_preview`` is true (``enabled``):
the dashboard then builds no ``Poller`` and issues no preview read at all.
Everything shown is sanitized first: pane output is untrusted, so escape
sequences (clipboard writes, title sets, cursor moves) and control characters
never reach the dashboard's own terminal.
"""
from __future__ import annotations

import re
import threading
import time
import unicodedata
from collections import OrderedDict, deque, namedtuple
from concurrent.futures import Future

INTERVAL = 0.5
MAX_LINES = 200
LINE_CHARS = 2000
EVENT_PAGE = 200
EVENT_PAGES = 5          # per read, so one catching-up read stays bounded
TAILS = 8                # structured cursors retained for recently selected rows
ENDED = frozenset({'closed', 'stopped', 'ended'})

Preview = namedtuple('Preview', 'key lines captured_at source note')


class Cancelled(Exception):
    """The dashboard closed while this read was queued or running."""

# ESC-introduced sequences: CSI, OSC (BEL or ST terminated, or cut off), the
# DCS/SOS/PM/APC string family, tmux's ESC k title, and two-byte escapes; then
# their 8-bit C1 forms. An unterminated string runs to the end of the text.
_ESCAPES = re.compile(r"""
    \x1b\[[0-?]*[ -/]*[@-~]?
  | \x1b[\]PX^_k][^\x07\x1b\x9c]*(?:\x07|\x1b\\|\x9c)?
  | \x1b[ -/]*[0-~]?
  | \x9b[0-?]*[ -/]*[@-~]?
  | [\x90\x98\x9d\x9e\x9f][^\x07\x1b\x9c]*(?:\x07|\x1b\\|\x9c)?
""", re.VERBOSE)


def sanitize(text):
    """Printable text only: escape sequences and control/format characters removed, tabs as a space."""
    text = _ESCAPES.sub('', str(text)).replace('\t', ' ')
    return ''.join(ch for ch in text if ch.isprintable() and unicodedata.category(ch) not in {'Cf', 'Cs'})


def _clean(line):
    return sanitize(line[:LINE_CHARS * 2])[:LINE_CHARS]


def enabled(config):
    """Whether the operator opted in to the preview; only a literal true does."""
    return getattr(config, 'session_preview', False) is True


def source_of(row):
    """'pane', 'events', 'ended' or None (nothing to preview) for a presented row."""
    if not row or row.get('kind') == 'section':
        return None
    transport = row.get('transport')
    if transport == 'structured':
        return 'events'
    if transport in {'terminal', 'room'}:
        if row.get('lifecycle') in ENDED or row.get('activity') == 'exited':
            return 'ended'
        return 'pane' if row.get('room_id') else None
    return None


def preview_key(row):
    """What a preview belongs to: a new generation or Room is a new screen."""
    return (row.get('session_id'), row.get('transport'), row.get('room_id'), row.get('generation'))


class StructuredTail:
    """A retained cursor into one structured session's events. Reads never name a consumer."""

    def __init__(self):
        self.cursor, self.turn = 0, None
        self.lines, self.partial = deque(maxlen=MAX_LINES), ''

    def _flush(self):
        if self.partial:
            self.lines.append(_clean(self.partial))
            self.partial = ''

    def _text(self, chunk):
        pieces = (self.partial + str(chunk)).split('\n')
        self.partial = pieces.pop()[-LINE_CHARS * 2:]
        self.lines.extend(_clean(piece) for piece in pieces[-MAX_LINES:])

    def _add(self, event):
        payload = event.get('payload') if isinstance(event.get('payload'), dict) else {}
        kind = event.get('kind')
        if event.get('turn_id') != self.turn:
            self._flush()
            self.turn = event.get('turn_id')
        if kind == 'text':
            self._text(payload.get('text', ''))
            return
        summary = {'tool': lambda: f"• {payload.get('name', 'tool')} {payload.get('status', '')}".rstrip(),
                   'request-opened': lambda: f"? {payload.get('question', 'input requested')}",
                   'turn-finished': lambda: f"— turn {payload.get('outcome', 'finished')}"}.get(kind)
        if summary:
            self._flush()
            self.lines.append(_clean(summary()))

    def read(self, store, sid, cancelled=None):
        """Advance through at most ``EVENT_PAGES`` pages; return the retained tail."""
        behind = False
        for _ in range(EVENT_PAGES):
            if cancelled is not None and cancelled.is_set():
                raise Cancelled()
            page = store.events(sid, after=self.cursor, limit=EVENT_PAGE)
            for event in page.get('events', []):
                self._add(event)
            self.cursor = page.get('next_event_cursor', self.cursor) or self.cursor
            behind = not page.get('complete', True)
            if not behind:
                break
        # A long history is read a bounded slice per tick; say so rather than pass old text off as live.
        return list(self.lines) + ([_clean(self.partial)] if self.partial else []) + \
            (['… reading older events'] if behind else [])


def reader_for(config, tmux=None, *, cancelled=None):
    """The production read: a pane capture or a structured event tail, never a write.

    ``cancelled`` is the ``Poller``'s event: once set, the read stops at its next step.
    """
    def read(row, lines, tail):
        if source_of(row) == 'events':
            from .session_store import SessionStore
            with SessionStore(config) as store:
                return tail.read(store, row['session_id'], cancelled)[-lines:]
        from . import pane_peek
        from .rooms import RoomStore
        from .tmux import TmuxAdapter
        record = RoomStore(config).read(row['room_id'])
        return [_clean(line) for line in pane_peek.peek_room(record, tmux or TmuxAdapter(), lines,
                                                             cancelled=cancelled)]
    return read


class DaemonPool:
    """Runs each read on its own daemon thread, so interpreter exit never waits for a stuck tmux read."""

    def submit(self, fn, *args):
        future = Future()

        def run():
            try:
                future.set_result(fn(*args))
            except BaseException as exc:  # noqa: BLE001 - delivered to the UI thread as a note
                future.set_exception(exc)
        threading.Thread(target=run, name='asha-session-preview', daemon=True).start()
        return future

    def shutdown(self, **kwargs):
        pass


class Poller:
    """Schedules preview reads for the selected row; the UI thread only ticks and reads results."""

    def __init__(self, *, reader, pool=None, clock=time.monotonic, wall=time.time, interval=INTERVAL,
                 cancelled=None):
        self._reader, self._clock, self._wall, self._interval = reader, clock, wall, interval
        self._pool = pool or DaemonPool()
        # Shared with the reader (``reader_for(cancelled=...)``) so close reaches a running read.
        self._cancelled = cancelled or threading.Event()
        self._closed = False
        self._inflight = None          # (key, future)
        self._latest = None            # Preview for the key it was read for
        self._next = 0.0
        self._tails = OrderedDict()

    def _tail(self, key):
        tail = self._tails.pop(key, None) or StructuredTail()
        self._tails[key] = tail
        while len(self._tails) > TAILS:
            self._tails.popitem(last=False)
        return tail

    def _collect(self, key):
        if self._inflight is None or not self._inflight[1].done():
            return
        done_key, future = self._inflight
        self._inflight = None
        try:
            finished, lines = future.result()
        except Exception as exc:  # noqa: BLE001 - any failure is shown, never raised into the UI
            finished = self._clock()
            from .pane_peek import PeekDisabled
            note = sanitize(exc) if isinstance(exc, PeekDisabled) else 'Preview unavailable: ' + sanitize(exc)
            result = Preview(done_key, [], None, 'error', note)
        else:
            result = Preview(done_key, [_clean(line) for line in lines][-MAX_LINES:], self._wall(),
                             'pane' if done_key[1] != 'structured' else 'events', '')
        # Pace from the end of the read: the next capture cannot follow this one closely.
        self._next = max(self._next, finished + self._interval)
        if done_key == key:
            self._latest = result    # a read for a selection that moved on is never shown

    def _timed(self, row, lines, tail):
        if self._cancelled.is_set():
            raise Cancelled()
        lines = self._reader(row, lines, tail)
        return self._clock(), lines

    def tick(self, row, *, lines):
        """Collect a finished read and start the next one when due."""
        if self._closed:
            return
        source = source_of(row)
        key = preview_key(row) if source else None
        self._collect(key)
        if source not in {'pane', 'events'} or self._inflight is not None or self._clock() < self._next:
            return
        self._next = self._clock() + self._interval
        tail = self._tail(key) if source == 'events' else None
        self._inflight = (key, self._pool.submit(self._timed, dict(row), max(1, min(lines, MAX_LINES)), tail))

    def current(self, row):
        """The preview to paint for this row, or None while its first read is pending."""
        source = source_of(row)
        if source == 'ended':
            return Preview(preview_key(row), [], None, 'ended', 'Session ended; no live screen')
        if source is None:
            return None
        latest = self._latest
        return latest if latest is not None and latest.key == preview_key(row) else None

    def close(self):
        """Stop scheduling and cancel a read still running; its result is never shown."""
        self._cancelled.set()
        self._closed, self._inflight = True, None
        self._pool.shutdown(wait=False, cancel_futures=True)
