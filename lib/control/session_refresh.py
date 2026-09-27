"""Change-driven dashboard refresh (#102 phase 4). No curses.

Every FAST_SECONDS the UI thread asks one long-lived read connection whether
any other connection committed (``PRAGMA data_version``). Unchanged, it does
nothing else. Changed, it collects the sessions written since its cursors:
hub rows by ``updated_at``, structured sessions by ``session_events.sequence``
and ``managed_sessions.updated_at``. The single worker then re-shows only
those rows and returns them as deltas.

Several facts change because time passed or a process died, and neither
writes a row: liveness, "hooks not reporting", staleness windows, receipt
staleness from a Memory publication, legacy Rooms. So every SLOW_SECONDS the
worker re-reads the whole page with one bounded tmux inventory, which later
row reads reuse.

The view is read-only. Its positions live only in this process; it writes
nothing back to the store and marks no event as seen.
"""
from __future__ import annotations

import sqlite3
import time
from collections import namedtuple

from . import session_view
from .hub_cli import overview, terminal_inventory
from .session_hub import Hub, listed

FAST_SECONDS = 0.25
SLOW_SECONDS = 5.0
# A change with no single-row read (a legacy managed session, a pruned row)
# brings the page forward, but never sooner than this after the last one.
EARLY_SECONDS = 2.0   # never more often than the old periodic refresh
# Stamps written this close below the cursor are compared, not trusted: two
# writers can share an instant, a writer stamps before it commits, and a wall
# clock can step back. A change older than this still shows on the slow tick.
WINDOW_SECONDS = 1.0
# More changed ids than this in one tick is cheaper to answer with a page.
ID_LIMIT = 1000
READ_SECONDS = 2.0

# The state indexes cover (updated_at, session_id), so this reads no payloads.
HUB_CHANGES = 'SELECT session_id, updated_at FROM hub_sessions WHERE updated_at >= ? LIMIT ?'
MANAGED_CHANGES = 'SELECT session_id, updated_at FROM managed_sessions WHERE updated_at >= ? LIMIT ?'
EVENT_CHANGES = 'SELECT session_id, sequence FROM session_events WHERE sequence > ? LIMIT ?'
TABLES = ('hub_sessions', 'managed_sessions', 'session_events')

Changes = namedtuple('Changes', 'ids everything')


def _monotonic():
    return time.monotonic()


def _wall():
    return time.time()


class _Stamps:
    """A cursor over a timestamp column: the highest stamp seen, and the ids near it."""

    def __init__(self, table):
        self.table, self.mark, self.recent = table, None, {}

    def prime(self, c, sql):
        """Start at the newest stamp, remembering the rows already near it."""
        self.mark = c.execute(f'SELECT MAX(updated_at) FROM {self.table}').fetchone()[0]
        self.recent = {}
        self.read(c, sql)

    def read(self, c, sql):
        """Ids whose stamp changed since the last read; None when there are too many to list."""
        floor = float('-inf') if self.mark is None else self.mark - WINDOW_SECONDS
        # Rows already known near the cursor come back too; they do not count against the limit.
        limit = len(self.recent) + ID_LIMIT + 1
        found = c.execute(sql, (floor, limit)).fetchall()
        changed = {sid for sid, at in found if self.recent.get(sid) != at}
        for sid, at in found:
            self.recent[sid] = at
            self.mark = at if self.mark is None else max(self.mark, at)
        if len(found) >= limit:
            # Truncated: rows past the limit are unseen, so move past them all.
            top = c.execute(f'SELECT MAX(updated_at) FROM {self.table}').fetchone()[0]
            self.mark = top if self.mark is None or top is None else max(self.mark, top)
        if self.mark is not None:
            self.recent = {sid: at for sid, at in self.recent.items() if at >= self.mark - WINDOW_SECONDS}
        return None if len(found) >= limit or len(changed) > ID_LIMIT else changed


class ChangeFeed:
    """One long-lived read connection; ``poll`` says which sessions changed."""

    def __init__(self, opener, *, clock=None):
        self._opener, self.clock = opener, clock or _monotonic
        self._db, self._version, self._retry_at = None, None, float('-inf')
        self._hub, self._managed, self._sequence = _Stamps('hub_sessions'), _Stamps('managed_sessions'), None

    @classmethod
    def for_config(cls, config, *, clock=None):
        from .database import ControlDatabase
        return cls(lambda: ControlDatabase(config, read_only=True, busy_timeout=0.2), clock=clock)

    def poll(self):
        """None when nothing committed (or the feed is down); else the changed ids.

        ``everything`` asks for a page: the feed just (re)opened and cannot
        know what it missed, or one tick changed more rows than it lists.
        """
        if self._db is None:
            return self._open()
        try:
            version = self._db.data_version()
            if version == self._version:
                return None
            self._version = version
            return self._changes()
        except (ValueError, OSError, sqlite3.Error):
            self._drop()
            return None

    def _open(self):
        if self.clock() < self._retry_at:
            return None
        self._retry_at = self.clock() + SLOW_SECONDS
        try:
            self._db = self._opener()
            self._version = self._db.data_version()
            self._prime()
        except Exception:  # noqa: BLE001  (the feed is an optimisation; the slow page is the authority)
            self._drop()
            return None
        return Changes(frozenset(), True)

    def _tables(self, c):
        marks = ','.join('?' * len(TABLES))
        return {row[0] for row in c.execute(
            f"SELECT name FROM sqlite_master WHERE type='table' AND name IN ({marks})", TABLES)}

    def _prime(self):
        with self._db.transaction() as c:
            tables = self._tables(c)
            if 'hub_sessions' in tables:
                self._hub.prime(c, HUB_CHANGES)
            if 'managed_sessions' in tables:
                self._managed.prime(c, MANAGED_CHANGES)
            if 'session_events' in tables:
                self._sequence = c.execute('SELECT MAX(sequence) FROM session_events').fetchone()[0] or 0

    def _changes(self):
        ids, everything = set(), False
        with self._db.transaction() as c:
            tables = self._tables(c)
            for name, stamps, sql in (('hub_sessions', self._hub, HUB_CHANGES),
                                      ('managed_sessions', self._managed, MANAGED_CHANGES)):
                if name in tables:
                    changed = stamps.read(c, sql)
                    everything |= changed is None
                    ids |= changed or set()
            if 'session_events' in tables:
                found = c.execute(EVENT_CHANGES, (self._sequence or 0, ID_LIMIT + 1)).fetchall()
                if len(found) > ID_LIMIT:
                    everything = True
                    self._sequence = c.execute('SELECT MAX(sequence) FROM session_events').fetchone()[0] or 0
                else:
                    ids |= {sid for sid, _ in found}
                    self._sequence = max([self._sequence or 0, *(sequence for _, sequence in found)])
        if not ids and not everything:
            return None
        return Changes(frozenset(ids), everything)

    def _drop(self):
        db, self._db, self._version = self._db, None, None
        self._retry_at = self.clock() + SLOW_SECONDS
        if db is not None:
            try:
                db.close()
            except (ValueError, OSError, sqlite3.Error):
                pass

    def close(self):
        self._drop()


def page_reader(config, env):
    """The slow read: one bounded inventory, then the whole page with it."""
    def read(include_closed):
        deadline = time.monotonic() + READ_SECONDS
        tmux, errors = terminal_inventory(deadline)
        page = overview(config, env=env, tmux=tmux, include_closed=include_closed,
                        tmux_errors=errors, deadline=deadline)
        # Kept beside the page for later row reads; never part of the listing.
        return dict(page, _inventory=None if errors else tmux)
    return read


def row_reader(config, env, *, hub_factory=None, inventory=None):
    """The delta read: re-show only the given ids, reusing ``tmux`` when given."""
    make_hub, take = hub_factory or Hub, inventory or terminal_inventory

    def read(ids, tmux):
        deadline = time.monotonic() + READ_SECONDS
        taken = None
        if tmux is None:
            tmux, errors = take(deadline)
            taken = None if errors else tmux
        hub = make_hub(config, env=env, tmux=tmux)
        result = dict(rows={}, failed={}, unowned=set(), deferred=set(), inventory=taken)
        for sid in sorted(ids):
            if time.monotonic() >= deadline:
                result['deferred'].add(sid)
                continue
            try:
                if not hub.owns(sid):
                    result['unowned'].add(sid)
                    continue
                result['rows'][sid] = hub.show(sid)
            except (ValueError, OSError) as exc:
                result['failed'][sid] = str(exc)[:200]
        return result
    return read


class _Job:
    def __init__(self, kind, future, *, observed_at, started, ids=frozenset()):
        self.kind, self.future, self.observed_at, self.started, self.ids = kind, future, observed_at, started, ids


class Refresher:
    """Schedules the fast change check, row deltas and the slow page on one worker."""

    def __init__(self, pool, *, read_page, read_rows, feed=None, clock=None, wall=None):
        self.pool, self.read_page, self.read_rows, self.feed = pool, read_page, read_rows, feed
        self.clock, self.wall = clock or _monotonic, wall or _wall
        self.job, self.pending = None, set()
        self.next_fast, self.next_slow, self.requested = float('-inf'), float('-inf'), False
        self.page_started = float('-inf')
        self.inventory = None   # (inventory, clock time it was read)

    def request_page(self):
        """Read the whole page as soon as the worker is free (a new query, an unowned row)."""
        self.requested = True

    def tick(self, view, *, include_closed):
        """One loop pass: returns (view, page or None, message or None)."""
        now = self.clock()
        if self.feed is not None and now >= self.next_fast:
            self.next_fast = now + FAST_SECONDS
            changes = self.feed.poll()
            if changes is not None:
                self.pending |= changes.ids
                if changes.everything:
                    self.request_page()
        if self.job is None:
            self._start(view, now, include_closed)
        if self.job is not None and self.job.future.done():
            return self._finish(view, include_closed)
        return view, None, None

    def _start(self, view, now, include_closed):
        if self.requested or now >= self.next_slow:
            # Measured from the start, so a page's own duration never stretches the
            # interval; one that outlasts it is followed at once, never overlapped.
            self.requested, self.page_started, self.next_slow = False, now, now + SLOW_SECONDS
            # The page observes everything written before it starts.
            self.pending.clear()
            self.job = _Job('page', self.pool.submit(self.read_page, include_closed),
                            observed_at=self.wall(), started=now)
        elif self.pending and self.read_rows is not None:
            ids, self.pending = frozenset(self.pending), set()
            self.job = _Job('rows', self.pool.submit(self.read_rows, ids, self._reusable(view, ids, now)),
                            observed_at=self.wall(), started=now, ids=ids)

    def _reusable(self, view, ids, now):
        """The last inventory, unless a row is new to the view or the sample is old."""
        if self.inventory is None or any(sid not in view.rows for sid in ids):
            return None
        tmux, taken = self.inventory
        return tmux if now - taken <= 2 * SLOW_SECONDS else None

    def _finish(self, view, include_closed):
        job, self.job = self.job, None
        if job.kind == 'page':
            try:
                page = job.future.result()
                inventory = page.get('_inventory')
                page = {key: value for key, value in page.items() if key != '_inventory'}
                view = session_view.merge(view, page['rows'], observed_at=job.observed_at,
                                          complete=bool(page.get('complete')))
            except Exception as exc:  # noqa: BLE001  (a failed read keeps every row, marked stale)
                return session_view.merge(view, [], observed_at=job.observed_at, complete=False), None, \
                    'Status unavailable: ' + str(exc)
            if inventory is not None:
                self.inventory = (inventory, job.started)
            return view, page, None
        try:
            result = job.future.result()
        except Exception as exc:  # noqa: BLE001
            return session_view.mark_stale(view, job.ids), None, 'Status unavailable: ' + str(exc)
        view = session_view.merge_changed(view, result['rows'].values(), observed_at=job.observed_at,
                                          member=lambda row: listed(row, include_closed=include_closed))
        view = session_view.mark_stale(view, result['failed'])
        self.pending |= result['deferred']
        if result['unowned']:
            self.next_slow = min(self.next_slow, max(self.clock(), self.page_started + EARLY_SECONDS))
        if result['inventory'] is not None:
            self.inventory = (result['inventory'], job.started)
        return view, None, None

    def close(self):
        if self.feed is not None:
            self.feed.close()
