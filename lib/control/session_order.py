"""Always-on ordering of native hook observations for hub sessions (#101, QA7/QA8).

Every hook takes the next number from a private per-(session, generation)
counter when it starts (``control-event.sh``, under flock, no tmux). The number
is the event's place in native order; hooks run as independent processes, so
their reports can arrive out of order, late, twice, or never.

The hub applies an event's activity, tool and background effects only when its
number is newer than the last applied one. A late or duplicate report is logged
without those effects (it can still invalidate a completion receipt: see
``ignored_work``). Numbers skipped by a newer report are kept as ``missing``
until their reports arrive.

One invariant guards every turnless termination, receipt close and
``--no-handoff`` alike (``kill_refusal``), checked while the counter's flock is
held, inside the session's observation lock, through the kill itself:

(a) the counter's allocated value equals the applied order, so a hook that took
    a number but has not reported (slow, killed, lost) refuses the kill;
(b) the last applied event is a Stop (tools and background work are checked by
    the callers' idle predicates);
(c) no unsequenced or out-of-order evidence stands: an unsequenced, late,
    duplicate or gapped report sets a barrier at the counter value when it
    arrived, and only an applied Stop numbered above that barrier (allocated
    after the evidence), or a new generation, clears it. Evidence that arrives
    while the counter cannot be read leaves the barrier pending; the next
    report that can read it fixes the barrier there (QA9 Q9-F2);
(d) every hook that started has been accounted for (QA9 Q9-F1). Before taking a
    number each hook appends one byte to this incarnation's private attempt
    log (O_APPEND, no lock), so a hook that then fails to allocate still leaves
    evidence even if its report never arrives. A numbered hook reports the
    log's size read under the counter lock; the kill requires the log's size
    now to equal the size the last applied Stop reported. Attempts before that
    Stop's allocation are covered by it, as in (c); any later attempt refuses.

This is independent of the experimental pane sequence that fences idle typing
(#96); that feature and its counters are untouched.
"""
from __future__ import annotations

import fcntl
import os
import re
import stat
import time
from contextlib import contextmanager
from pathlib import Path
from typing import NamedTuple

from .store import StoreError

ORDER_LIMIT = 10 ** 9
LOG_LIMIT = 16
MISSING_LIMIT = 64
# The attempt log grows one byte per hook; the hook stops appending here and
# every turnless kill then refuses until a new generation (resume).
ATTEMPT_LIMIT = 4 * 1024 * 1024
ENV = 'ASHA_HUB_EVENT_ORDER'
_CONTENT = re.compile(rb'(0|[1-9][0-9]{0,8})\n?')
# Tool kinds whose events never count as new work (existing epoch rules).
_REPORT_KINDS = {'report'}


def counter_path(config, sid: str, generation: int) -> Path:
    return config.tasks_dir.parent / 'hub-event-order' / sid / str(generation)


def attempts_path(config, sid: str, generation: int) -> Path:
    """The incarnation's attempt log: the hook derives it as ``$ASHA_HUB_EVENT_ORDER.attempts``."""
    path = counter_path(config, sid, generation)
    return path.with_name(path.name + '.attempts')


class Counters(NamedTuple):
    """One locked reading: numbers allocated, and hooks started (None when the log is unusable)."""
    allocated: int
    attempts: int | None


def _valid_counter(fd) -> bool:
    metadata = os.fstat(fd)
    return (stat.S_ISREG(metadata.st_mode) and metadata.st_uid == os.geteuid()
            and not stat.S_IMODE(metadata.st_mode) & 0o077)


def create_counter(config, sid: str, generation: int) -> str:
    """Create this incarnation's counter (0600, content ``0``) and its empty attempt log (0600)
    in a private directory; return the counter's path.

    An existing file is accepted only when it is a private regular file of this
    user with valid content; anything else refuses the launch.
    """
    from .store import _directory_fd, _managed_start
    path = counter_path(config, sid, generation)
    root = path.parent
    with _directory_fd(root, create=True,
                       managed_start=_managed_start(root, ('control', 'hub-event-order', sid))) as fd:
        try:
            handle = os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        except FileExistsError:
            try:
                handle = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
            except OSError as exc:
                raise StoreError(f'event order counter is not a regular file: {path}') from exc
            try:
                if not _valid_counter(handle) or not _CONTENT.fullmatch(os.pread(handle, 32, 0)):
                    raise StoreError(f'event order counter is invalid: {path}')
            finally:
                os.close(handle)
        else:
            try:
                os.write(handle, b'0\n')
            finally:
                os.close(handle)
        log = attempts_path(config, sid, generation).name
        try:
            handle = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        except FileExistsError:
            try:
                handle = os.open(log, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
            except OSError as exc:
                raise StoreError(f'event attempt log is not a regular file: {path}.attempts') from exc
            try:
                if not _valid_counter(handle):
                    raise StoreError(f'event attempt log is invalid: {path}.attempts')
            finally:
                os.close(handle)
        else:
            os.close(handle)
    return str(path)


def _attempts(path) -> int | None:
    """The attempt log's size, or None when it is missing, not private, or not a regular file."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        return os.fstat(fd).st_size if _valid_counter(fd) else None
    finally:
        os.close(fd)


def _flock(fd, wait):
    deadline = time.monotonic() + wait
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.005)


@contextmanager
def allocation(config, row, *, wait=1.0):
    """Yield this incarnation's ``Counters`` while holding the counter's flock.

    Yields None when the counter is missing, invalid or its lock is not taken
    within ``wait`` seconds. While held, no hook of this incarnation can take a
    number: a hook that starts meanwhile times out its own short wait and
    reports unsequenced, but its attempt is already in the log.
    """
    path = counter_path(config, row['session_id'], row['generation'])
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        yield None
        return
    try:
        if not _valid_counter(fd):
            yield None
            return
        if not _flock(fd, wait):
            yield None
            return
        match = _CONTENT.fullmatch(os.pread(fd, 32, 0))
        yield (Counters(int(match.group(1)), _attempts(attempts_path(config, row['session_id'], row['generation'])))
               if match else None)
    finally:
        os.close(fd)


def read_counters(config, row, *, wait=0.5):
    with allocation(config, row, wait=wait) as counters:
        return counters


def read_allocated(config, row, *, wait=0.5):
    counters = read_counters(config, row, wait=wait)
    return None if counters is None else counters.allocated


def validate(order, attempts=None):
    if order is not None and (type(order) is not int or not 0 < order < ORDER_LIMIT):
        raise StoreError('invalid event order')
    # Concurrent hooks can pass ATTEMPT_LIMIT by a few bytes; kill_refusal refuses a full log,
    # so the report itself is still accepted.
    if attempts is not None and (order is None or type(attempts) is not int or not 0 < attempts < ORDER_LIMIT):
        raise StoreError('invalid event attempt count')
    return order


def state(row) -> dict:
    """The row's order state for its current incarnation (a new generation starts fresh)."""
    current = row.get('event_order') or {}
    if current.get('generation') != row['generation']:
        return dict(generation=row['generation'], applied=0, last_event=None, missing=[], barrier=None)
    return dict(current, missing=list(current.get('missing') or []))


def would_invalidate(event, tool_kind):
    """Whether this event, had it applied, would count as new work (the existing epoch rules)."""
    if event == 'prompt-submitted':
        return True
    if event == 'tool-started':
        return tool_kind not in _REPORT_KINDS
    if event == 'tool-completed':
        return tool_kind not in _REPORT_KINDS | {'finalizer'}
    return False


def _raise_barrier(current, allocated):
    """Out-of-order or unordered evidence: only a Stop numbered above the counter now clears it.

    When the counter cannot be read now (lock contention, most often) the
    barrier stays pending instead of becoming unclearable: the next report that
    reads the counter fixes it there, a value at least as high as the evidence's.
    """
    if allocated is None:
        current['barrier_pending'] = True
    else:
        current['barrier'] = max(current['barrier'] or 0, allocated)


def _resolve_pending(current, allocated):
    if current.get('barrier_pending'):
        value = allocated()
        if value is not None:
            current['barrier_pending'] = False
            current['barrier'] = max(current['barrier'] or 0, value)


def _skip(current, applied, order):
    """Record numbers ``applied+1 .. order-1`` as missing, keeping at most MISSING_LIMIT of them.

    Only the newest are kept, and never more are materialized: a gap may be up
    to ORDER_LIMIT wide (QA9 H1). The highest number dropped is kept instead, so
    a receipt issued after it is unaffected by the dropped reports (QA9 Q9-F3).
    """
    tail = max(applied + 1, order - MISSING_LIMIT)
    missing = current['missing'] + list(range(tail, order))
    dropped = missing[:-MISSING_LIMIT]
    if tail > applied + 1:
        dropped.append(tail - 1)
    if dropped:
        current['missing_dropped'] = max([current.get('missing_dropped') or 0, *dropped])
    current['missing'] = missing[-MISSING_LIMIT:]


def observe(row, event: str, order, *, allocated=lambda: None, attempts=None, now=None):
    """Return ``(disposition, fields)``: 'applied' or 'ignored', plus the row fields to write.

    ``allocated()`` reads the counter when the report is out of order or
    unsequenced; the evidence is known to precede any number above that value.
    ``attempts`` is the attempt log size the hook read under the counter lock
    before taking its number, so it never covers a hook that appended later.
    """
    now = time.time() if now is None else now
    current = state(row)
    _resolve_pending(current, allocated)
    if order is None:
        _raise_barrier(current, allocated())
        return 'applied', dict(event_order=current)
    applied = current['applied']
    if order <= applied:
        filled = order in current['missing']
        if filled:
            current['missing'].remove(order)
        _raise_barrier(current, allocated())
        entry = dict(event=event, order=order, applied=applied, generation=row['generation'], at=now,
                     ignored='late' if filled or order < applied else 'duplicate')
        return 'ignored', dict(event_order=current,
                               observation_log=(row.get('observation_log') or [])[-(LOG_LIMIT - 1):] + [entry])
    if order > applied + 1:
        # Reports for these numbers have not arrived: they may be in flight.
        _raise_barrier(current, allocated())
        _skip(current, applied, order)
    current.update(applied=order, last_event=event, attempts=attempts)
    if (event == 'turn-stopped' and current['barrier'] is not None and order > current['barrier']
            and not current.get('barrier_pending')):
        current['barrier'] = None
    return 'applied', dict(event_order=current)


def kill_refusal(row, counters):
    """Why a turnless kill of this live terminal is not proven safe now, or None (invariant a-d)."""
    current = state(row)
    if counters is None:
        return ('the native event counter of this incarnation is unavailable (sessions launched before '
                'ordering, or a missing/invalid counter); resume, attach, or --force')
    allocated, attempts = counters
    if attempts is None:
        return ('the native event attempt log of this incarnation is unavailable (missing, or not a private '
                'file); resume, attach, or --force')
    if attempts >= ATTEMPT_LIMIT:
        return ('the native event attempt log of this incarnation is full; no Stop can clear it in this '
                'generation. Stop then resume the session for a fresh log, or close --force (no save claim)')
    if not current['applied']:
        return 'no ordered native event has been applied for this incarnation'
    if allocated > current['applied']:
        return (f"{allocated - current['applied']} native event report(s) were started but have not arrived "
                '(in flight, or lost); wait for the next Stop, attach, or --force')
    if allocated < current['applied']:
        return 'the native event counter is behind the applied order; attach, or --force'
    if current['last_event'] != 'turn-stopped':
        return 'the last ordered native event is not a Stop'
    if current.get('barrier_pending'):
        return ('an unsequenced native event report arrived while the event counter was unreadable; the next '
                'ordered report resolves it and a Stop after that clears it; wait for the next Stop, attach, or --force')
    if current['barrier'] is not None:
        return ('an unsequenced or out-of-order native event report arrived and no Stop allocated after it '
                'has been applied; wait for the next Stop, attach, or --force')
    covered = current.get('attempts')
    if covered is None:
        return ('the last Stop did not report the attempt log; wait for the next Stop, attach, or --force')
    if attempts != covered:
        return (f'{abs(attempts - covered)} native hook(s) started since the last Stop took its number and '
                'have no ordered report (lock contention, a timeout, or lost); wait for the next Stop, '
                'attach, or --force')
    return None


def receipt_unaccounted(row, receipt, counters):
    """Why events after this receipt are unaccounted for (reports not yet arrived), or None."""
    mark = receipt.get('order_applied')
    current = state(row)
    if mark is None or receipt.get('generation') != row['generation']:
        return 'the receipt carries no native event order'
    if ((current.get('missing_dropped') or 0) > mark
            or any(order > mark for order in current['missing'])):
        return 'a native event report after the receipt has not arrived'
    allocated = None if counters is None else counters.allocated
    if allocated is not None and allocated > current['applied']:
        return 'a native event started after the receipt has not reported'
    return None


def ignored_work(row, event, order, tool_kind):
    """True when an ignored late report is work after the row's ready receipt (d)."""
    receipt = row.get('completion') or {}
    mark = receipt.get('order_applied')
    return (receipt.get('status') == 'ready' and receipt.get('generation') == row['generation']
            and (mark is None or order > mark) and would_invalidate(event, tool_kind))
