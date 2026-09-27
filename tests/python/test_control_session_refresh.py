"""#102 phase 4: change-driven dashboard refresh.

A 250 ms ``PRAGMA data_version`` tick on one long-lived read connection does
nothing while nothing committed; a commit re-shows only the rows written since
the cursor; a 5 s slow tick re-reads the page for facts that only time reveals.
"""
import collections
import time
import unittest
from concurrent.futures import Future
from pathlib import Path
from unittest import mock

from lib.control import hub_cli, session_refresh, session_view
from lib.control.database import ControlDatabase, DatabaseError
from lib.control.session_hub import HOOK_SILENCE_SECONDS
from tests.python.test_control_session_closure import ACTIVE, ClosureFixture


class Clock:
    def __init__(self, now=0.0):
        self.now = now

    def __call__(self):
        return self.now


class InlinePool:
    """The single worker, run at submission so a test observes each job synchronously."""

    def __init__(self):
        self.jobs = []

    def submit(self, fn, *args, **kwargs):
        self.jobs.append(fn)
        future = Future()
        try:
            future.set_result(fn(*args, **kwargs))
        except Exception as exc:  # noqa: BLE001
            future.set_exception(exc)
        return future


class ManualPool:
    """A worker whose jobs finish only when the test says so."""

    def __init__(self):
        self.jobs = []

    def submit(self, fn, *args, **kwargs):
        future = Future()
        self.jobs.append((future, fn, args, kwargs))
        return future

    def finish(self):
        future, fn, args, kwargs = self.jobs[-1]
        try:
            future.set_result(fn(*args, **kwargs))
        except Exception as exc:  # noqa: BLE001
            future.set_exception(exc)

    def in_flight(self):
        return sum(not future.done() for future, *_ in self.jobs)


class CountingHub:
    """Delegates to the real hub and counts every row read."""

    def __init__(self, hub):
        self.hub, self.shows = hub, collections.Counter()

    def __call__(self, config, env=None, tmux=None):
        return self

    def owns(self, sid):
        return self.hub.owns(sid)

    def show(self, sid):
        self.shows[sid] += 1
        return self.hub.show(sid)


class RefreshFixture(ClosureFixture):
    """A real Control database, a fake terminal and an injected clock."""

    def setUp(self):
        super().setUp()
        self.clock = Clock(100.0)
        self.wall = Clock(1_000_000.0)
        self.pages = 0
        self.counting = CountingHub(self.hub)

    def other(self, sid, name='Other', **changes):
        """A second hub row beside a launched one (the fake terminal holds one Room)."""
        import uuid
        from lib.control.session_hub import Hub
        row = dict(self.hub.get(sid), session_id=str(uuid.uuid4()), name=name, **changes)
        with self.hub.database() as db, db.transaction(write=True) as c:
            Hub._save(c, row)
        return row['session_id']

    def page(self, include_closed):
        self.pages += 1
        return hub_cli.overview(self.config, env=self.env, tmux=self.tmux, include_closed=include_closed)

    def feed(self):
        return session_refresh.ChangeFeed.for_config(self.config, clock=self.clock)

    def refresher(self, *, feed=None, pool=None, read_page=None):
        rows = session_refresh.row_reader(self.config, self.env, hub_factory=self.counting,
                                          inventory=lambda deadline: (self.tmux, []))
        return session_refresh.Refresher(pool or InlinePool(), read_page=read_page or self.page, read_rows=rows,
                                         feed=feed if feed is not None else self.feed(),
                                         clock=self.clock, wall=self.wall)

    def run_for(self, refresher, view, seconds, *, step=0.05, include_closed=False, each=None):
        """Advance the injected clocks, ticking like the loop's getch timeout."""
        end = self.clock.now + seconds
        while self.clock.now < end - 1e-9:
            self.clock.now += step
            self.wall.now += step
            view, _, _ = refresher.tick(view, include_closed=include_closed)
            if each:
                each(view)
        return view


class ChangeFeedTests(RefreshFixture):
    def test_unchanged_data_version_reports_nothing_and_reads_no_table(self):
        self.launch()
        feed = self.feed()
        self.assertTrue(feed.poll().everything)   # opening primes the cursors and asks for a page
        statements = []
        feed._db.set_trace(statements.append)
        for _ in range(20):
            self.assertIsNone(feed.poll())
        self.assertEqual(statements, ['PRAGMA data_version'] * 20)

    def test_a_write_from_another_connection_reports_exactly_that_row(self):
        one = self.launch()['session_id']
        two = self.other(one)
        feed = self.feed()
        feed.poll()
        self.hub._update(two, name='Renamed')
        changes = feed.poll()
        self.assertEqual(changes.ids, {two})
        self.assertFalse(changes.everything)
        self.assertIsNone(feed.poll())
        self.hub._update(one, name='Again')
        self.assertEqual(feed.poll().ids, {one})

    def test_equal_timestamps_are_compared_not_trusted(self):
        sid = self.launch()['session_id']
        feed = self.feed()
        feed.poll()
        stamp = self.hub.get(sid)['updated_at']
        # Another row written at the cursor's own instant is still a change.
        with mock.patch('lib.control.session_hub.time.time', return_value=stamp):
            other = self.other(sid)
        self.assertEqual(feed.poll().ids, {other})
        # So is a row written after a wall clock stepped back inside the window.
        with mock.patch('lib.control.session_hub.time.time', return_value=stamp - 0.5):
            self.hub._update(sid, name='Earlier instant')
        self.assertEqual(feed.poll().ids, {sid})

    def test_structured_events_and_managed_state_report_their_session(self):
        sid = '00000000-0000-4000-8000-000000000001'
        self.launch()
        from lib.control.session_store import SCHEMA
        with ControlDatabase(self.config) as db, db.transaction(write=True) as c:
            for statement in SCHEMA:
                c.execute(statement)
        feed = self.feed()
        feed.poll()
        with ControlDatabase(self.config) as db, db.transaction(write=True) as c:
            c.execute("INSERT INTO managed_sessions(session_id,harness,cwd,state,max_turns,created_at,updated_at)"
                      " VALUES(?,?,?,?,?,?,?)", (sid, 'codex', str(self.project), 'idle', 5, 1.0, 1.0))
        self.assertEqual(feed.poll().ids, {sid})
        with ControlDatabase(self.config) as db, db.transaction(write=True) as c:
            c.execute("INSERT INTO session_events(session_id,kind,payload,created_at) VALUES(?,?,?,?)",
                      (sid, 'text', '{}', 2.0))
        self.assertEqual(feed.poll().ids, {sid})
        self.assertIsNone(feed.poll())

    def test_the_change_query_reads_the_covering_index_not_payloads(self):
        self.launch()
        feed = self.feed()
        feed.poll()
        with feed._db.transaction() as c:
            plan = ' '.join(str(row[-1]) for row in c.execute(
                'EXPLAIN QUERY PLAN ' + session_refresh.HUB_CHANGES, (0.0, 10)))
        self.assertIn('COVERING INDEX', plan)

    def test_too_many_changes_ask_for_a_page_instead(self):
        sid = self.launch()['session_id']
        feed = self.feed()
        feed.poll()
        with mock.patch.object(session_refresh, 'ID_LIMIT', 2):
            for index in range(3):
                self.other(sid, name=f'Job {index}')
            self.assertTrue(feed.poll().everything)
            self.assertIsNone(feed.poll())
            self.other(sid, name='After the page')
            self.assertFalse(feed.poll().everything)

    def test_the_connection_is_read_only_and_never_consumes_events(self):
        self.launch()
        feed = self.feed()
        feed.poll()
        with self.assertRaises(DatabaseError), feed._db.transaction() as c:
            c.execute("UPDATE hub_sessions SET updated_at=0")
        with self.assertRaises(DatabaseError):
            feed._db.transaction(write=True).__enter__()
        source = Path(session_refresh.__file__).read_text()
        for forbidden in ('acknowledge', 'consumer', 'event_cursor', 'ack-events', 'write=True', 'INSERT', 'UPDATE ', 'DELETE'):
            self.assertNotIn(forbidden, source)

    def test_an_unavailable_database_retries_only_at_the_slow_period(self):
        opened = []
        def opener():
            opened.append(self.clock.now)
            raise DatabaseError('Control database does not exist')
        feed = session_refresh.ChangeFeed(opener, clock=self.clock)
        for _ in range(40):
            self.clock.now += session_refresh.FAST_SECONDS
            self.assertIsNone(feed.poll())
        self.assertEqual(len(opened), 2)
        self.assertGreaterEqual(opened[1] - opened[0], session_refresh.SLOW_SECONDS)

    def test_a_failed_read_reopens_later_and_asks_for_a_page(self):
        self.launch()
        feed = self.feed()
        feed.poll()
        feed._db.close()
        self.assertIsNone(feed.poll())
        self.clock.now += session_refresh.SLOW_SECONDS
        self.assertTrue(feed.poll().everything)


class RefresherTests(RefreshFixture):
    def test_no_show_when_data_version_is_unchanged(self):
        self.launch()
        refresher = self.refresher()
        view = self.run_for(refresher, session_view.ViewModel(), 0.1)
        self.assertEqual(self.pages, 1)
        self.assertEqual(sum(self.counting.shows.values()), 0)
        self.run_for(refresher, view, session_refresh.SLOW_SECONDS - 0.2)
        self.assertEqual(sum(self.counting.shows.values()), 0)
        self.assertEqual(self.pages, 1)

    def test_a_write_from_another_connection_triggers_exactly_one_row_refresh(self):
        one = self.launch()['session_id']
        two = self.other(one)
        refresher = self.refresher()
        view = self.run_for(refresher, session_view.ViewModel(), 0.1)
        self.assertEqual(set(view.rows), {one, two})
        self.hub._update(two, name='Renamed elsewhere')
        view = self.run_for(refresher, view, 1.0)
        self.assertEqual(self.counting.shows, collections.Counter({two: 1}))
        self.assertEqual(view.rows[two]['name'], 'Renamed elsewhere')
        self.assertEqual(self.pages, 1)

    def test_hooks_not_reporting_surfaces_within_one_slow_tick(self):
        sid = self.launch()['session_id']
        crossing = self.wall.now + 2.0
        self.hub._update(sid, launched_at=crossing - HOOK_SILENCE_SECONDS)
        with mock.patch('lib.control.session_hub.time.time', self.wall):
            refresher = self.refresher()
            view = self.run_for(refresher, session_view.ViewModel(), 0.1)
            self.assertNotEqual(view.rows[sid].get('telemetry'), 'hooks-not-reporting')
            seen = []
            self.run_for(refresher, view, 2 * session_refresh.SLOW_SECONDS, each=lambda v: seen.append(
                (self.wall.now, v.rows[sid].get('telemetry'))))
        first = next(at for at, telemetry in seen if telemetry == 'hooks-not-reporting')
        self.assertGreater(first, crossing)
        self.assertLessEqual(first - crossing, session_refresh.SLOW_SECONDS + 0.1)
        self.assertEqual(sum(self.counting.shows.values()), 0)   # no write, so no row read

    def test_a_slow_page_does_not_delay_a_time_derived_transition_past_one_interval(self):
        # Q27-F1: the first page observes the row just before the threshold, then takes
        # two seconds to finish. The next page must still start five seconds after the first.
        sid = self.launch()['session_id']
        crossing = self.wall.now + 0.01
        self.hub._update(sid, launched_at=crossing - HOOK_SILENCE_SECONDS)
        pool = TimedPool(self.clock, [2.0])
        with mock.patch('lib.control.session_hub.time.time', self.wall):
            refresher = self.refresher(pool=pool)
            view, seen = session_view.ViewModel(), []
            for _ in range(round(4 * session_refresh.SLOW_SECONDS / session_refresh.FAST_SECONDS)):
                pool.settle()
                view, _, _ = refresher.tick(view, include_closed=False)
                seen.append((self.wall.now, view.rows.get(sid, {}).get('telemetry')))
                self.clock.now += session_refresh.FAST_SECONDS
                self.wall.now += session_refresh.FAST_SECONDS
        first = next(at for at, telemetry in seen if telemetry == 'hooks-not-reporting')
        # Five seconds plus the next page's own (zero) duration and one collecting tick.
        self.assertLessEqual(first - crossing, session_refresh.SLOW_SECONDS + session_refresh.FAST_SECONDS)

    def test_a_quiet_dashboard_costs_one_pragma_per_fast_tick_and_a_page_per_slow_tick(self):
        self.other(self.launch()['session_id'])
        feed = self.feed()
        refresher = self.refresher(feed=feed)
        view = self.run_for(refresher, session_view.ViewModel(), 0.05)
        statements = []
        feed._db.set_trace(statements.append)
        pages = self.pages
        self.run_for(refresher, view, 60.0, step=0.2)   # the loop wakes on its getch timeout
        per_minute = collections.Counter(statements)
        self.assertEqual(set(per_minute), {'PRAGMA data_version'})
        self.assertLessEqual(per_minute['PRAGMA data_version'], 60 / session_refresh.FAST_SECONDS + 1)
        self.assertLessEqual(self.pages - pages, 60 / session_refresh.SLOW_SECONDS + 1)
        self.assertEqual(sum(self.counting.shows.values()), 0)

    def test_reading_rows_writes_nothing_so_the_view_never_wakes_itself(self):
        sid = self.launch()['session_id']
        refresher = self.refresher()
        view = self.run_for(refresher, session_view.ViewModel(), 0.1)
        self.hub._update(sid, name='Once')
        self.run_for(refresher, view, 3.0)
        self.assertEqual(self.counting.shows[sid], 1)

    def test_one_job_at_a_time_on_the_single_worker(self):
        sid = self.launch()['session_id']
        pool = ManualPool()
        refresher = self.refresher(pool=pool)
        view, _, _ = refresher.tick(session_view.ViewModel(), include_closed=False)
        self.assertEqual(pool.in_flight(), 1)
        self.hub._update(sid, name='While the page reads')
        self.clock.now += 1.0
        view, _, _ = refresher.tick(view, include_closed=False)
        self.assertEqual(pool.in_flight(), 1)   # the change waits for the worker
        pool.finish()
        self.clock.now += 0.3
        view, page, _ = refresher.tick(view, include_closed=False)
        self.assertIsNotNone(page)
        self.clock.now += 0.3
        view, _, _ = refresher.tick(view, include_closed=False)
        self.assertEqual(pool.in_flight(), 1)
        pool.finish()
        self.clock.now += 0.3
        view, page, _ = refresher.tick(view, include_closed=False)
        self.assertIsNone(page)   # a row delta is not a page
        self.assertEqual(view.rows[sid]['name'], 'While the page reads')
        self.assertEqual(len(pool.jobs), 2)

    def test_a_failed_row_read_marks_the_row_stale_and_keeps_it(self):
        one = self.launch()['session_id']
        two = self.other(one)
        refresher = self.refresher()
        view = self.run_for(refresher, session_view.ViewModel(), 0.1)
        self.hub._update(one, name='Changed')
        with mock.patch.object(self.counting, 'show', side_effect=OSError('database is locked')):
            view = self.run_for(refresher, view, 0.5)
        self.assertEqual(set(view.rows), {one, two})
        self.assertIn(one, view.stale)
        self.assertNotIn(two, view.stale)
        # The next page is fresh evidence and clears the marker.
        view = self.run_for(refresher, view, session_refresh.SLOW_SECONDS)
        self.assertNotIn(one, view.stale)

    def test_a_row_newer_than_the_read_is_kept(self):
        row = dict(session_id='a', generation=1, activity='idle', project_name='p', name='new',
                   harness='claude', transport='terminal', profile='worker', lifecycle='open',
                   process_state='live', reason='r')
        view = session_view.merge_row(session_view.ViewModel(), row, observed_at=10.0)
        older = dict(row, name='old')
        kept = session_view.merge_changed(view, [older], observed_at=5.0, member=lambda r: True)
        self.assertEqual(kept.rows['a']['name'], 'new')
        newer = session_view.merge_changed(view, [older], observed_at=11.0, member=lambda r: True)
        self.assertEqual(newer.rows['a']['name'], 'old')

    def test_a_change_without_a_single_row_read_brings_the_page_forward(self):
        refresher = self.refresher(feed=FakeFeed([None, session_refresh.Changes(frozenset({'legacy'}), False)]))
        view = self.run_for(refresher, session_view.ViewModel(), 0.1)
        self.assertEqual(self.pages, 1)
        view = self.run_for(refresher, view, session_refresh.EARLY_SECONDS + 0.3)
        self.assertEqual(self.pages, 2)

    def test_a_requested_page_during_a_read_is_not_lost(self):
        pool = ManualPool()
        refresher = self.refresher(pool=pool, feed=FakeFeed([]))
        view, _, _ = refresher.tick(session_view.ViewModel(), include_closed=False)
        refresher.request_page()
        pool.finish()
        view, _, _ = refresher.tick(view, include_closed=True)
        self.clock.now += 0.3
        refresher.tick(view, include_closed=True)
        # Q17-F6: the old page's completion does not swallow the new query.
        self.assertEqual([args for _, _, args, _ in pool.jobs], [(False,), (True,)])

    def test_without_a_feed_it_behaves_like_the_periodic_page(self):
        refresher = session_refresh.Refresher(InlinePool(), read_page=self.page, read_rows=None, feed=None,
                                              clock=self.clock, wall=self.wall)
        self.run_for(refresher, session_view.ViewModel(), 2 * session_refresh.SLOW_SECONDS + 0.3)
        self.assertEqual(self.pages, 3)

    def test_the_refresh_path_never_reads_a_preview(self):
        source = Path(session_refresh.__file__).read_text()
        for forbidden in ('pane_peek', 'session_preview', 'capture-pane', 'capture_pane'):
            self.assertNotIn(forbidden, source)


class TimedPool:
    """A worker whose jobs take an injected duration on the injected clock."""

    def __init__(self, clock, durations):
        self.clock, self.durations, self.jobs = clock, list(durations), []

    def submit(self, fn, *args, **kwargs):
        """The read observes at its start; its result is collectable only after its duration."""
        future = Future()
        due = self.clock.now + (self.durations.pop(0) if self.durations else 0.0)
        self.jobs.append((future, due, fn(*args, **kwargs)))
        return future

    def settle(self):
        for future, due, result in self.jobs:
            if not future.done() and self.clock.now >= due - 1e-9:
                future.set_result(result)

    def in_flight(self):
        return sum(not future.done() for future, *_ in self.jobs)


class SlowCadenceTests(unittest.TestCase):
    """Q27-F1: the slow page runs on a cadence from each page's start, never overlapping."""

    def cadence(self, durations, seconds):
        clock, starts, overlap = Clock(100.0), [], []
        pool = TimedPool(clock, durations)

        def read_page(include_closed):
            starts.append(clock.now)
            return dict(rows=[], complete=True)

        refresher = session_refresh.Refresher(pool, read_page=read_page, read_rows=None, feed=FakeFeed([]),
                                              clock=clock, wall=clock)
        view, end = session_view.ViewModel(), clock.now + seconds
        while clock.now < end - 1e-9:
            clock.now += session_refresh.FAST_SECONDS
            pool.settle()
            view, _, _ = refresher.tick(view, include_closed=False)
            overlap.append(pool.in_flight())
        return starts, overlap

    def test_page_duration_does_not_stretch_the_slow_interval(self):
        for duration in (0.25, 1.0, 2.0):
            with self.subTest(duration=duration):
                starts, _ = self.cadence([duration] * 20, 30.0)
                gaps = [b - a for a, b in zip(starts, starts[1:])]
                self.assertGreaterEqual(len(gaps), 4)
                for gap in gaps:
                    self.assertAlmostEqual(gap, session_refresh.SLOW_SECONDS, delta=1e-6)

    def test_a_page_longer_than_the_interval_is_never_overlapped(self):
        starts, overlap = self.cadence([7.0] * 20, 30.0)
        self.assertLessEqual(max(overlap), 1)
        gaps = [b - a for a, b in zip(starts, starts[1:])]
        # The next page waits for the worker, then starts at once: a tick is skipped, not doubled.
        for gap in gaps:
            self.assertGreaterEqual(gap, 7.0 - 1e-6)
            self.assertLessEqual(gap, 7.0 + session_refresh.FAST_SECONDS + 1e-6)


class RecordingTmux:
    """Records every terminal method the refresh path calls."""

    def __init__(self, tmux):
        self._tmux, self.calls = tmux, []

    def __getattr__(self, name):
        value = getattr(self._tmux, name)
        if callable(value):
            self.calls.append(name)
        return value


class DashboardRefreshTests(RefreshFixture):
    """The curses loop drives the refresher; keys never force a page."""

    def dashboard(self, config):
        from lib.control import session_title, session_tui, tui
        from tests.python.test_control_session_dashboard_keys import FakeCurses, Screen
        self.terminal = RecordingTmux(self.tmux)
        patches = [mock.patch.object(session_tui, 'ThreadPoolExecutor', return_value=mock.MagicMock(
                       submit=InlinePool().submit)),
                   mock.patch.object(session_tui, 'curses', FakeCurses),
                   mock.patch.object(session_tui, '_title_writer',
                                     return_value=session_title.TitleWriter(None, enabled=False)),
                   mock.patch.object(tui, 'init_colours', return_value=False),
                   mock.patch.object(session_refresh, 'terminal_inventory',
                                     side_effect=lambda deadline: (self.terminal, [])),
                   mock.patch.object(session_refresh, 'Hub', self.counting),
                   mock.patch('time.monotonic', self.clock)]
        for patcher in patches:
            self.enterContext(patcher)
        self.screen = Screen([], size=(30, 100))
        return session_tui.Dashboard(self.screen, config, self.env)

    def test_a_commit_elsewhere_refreshes_only_that_row_and_keys_read_nothing(self):
        one = self.launch()['session_id']
        two = self.other(one)
        dash = self.dashboard(self.config)
        dash.poll()
        self.assertEqual(set(dash.view.rows), {one, two})
        before = dash.view
        for key in (258, 259, ord('g'), ord('g'), ord('M'), ord('M')):
            self.clock.now += 0.05
            dash.handle(key)
            dash.poll()
        self.assertEqual(sum(self.counting.shows.values()), 0)
        self.assertEqual(set(dash.view.rows), set(before.rows))
        self.hub._update(two, name='Changed elsewhere')
        self.clock.now += session_refresh.FAST_SECONDS
        dash.poll()
        self.assertEqual(self.counting.shows, collections.Counter({two: 1}))
        self.assertEqual(dash.view.rows[two]['name'], 'Changed elsewhere')
        # With the preview off no pane is ever captured on this path.
        self.assertFalse([call for call in self.terminal.calls if 'capture' in call])
        dash.refresher.close()

    def test_history_toggle_reads_the_page_at_once(self):
        self.launch()
        dash = self.dashboard(self.config)
        dash.poll()
        pages = dash.refresher.page_started
        self.clock.now += 0.3
        dash.handle(ord('A'))
        dash.poll()
        self.assertGreater(dash.refresher.page_started, pages)
        dash.refresher.close()

    def test_the_loop_wakes_on_the_fast_tick(self):
        from lib.control import session_tui
        timeouts = []
        dash = self.dashboard(self.config)
        self.screen.timeout = timeouts.append
        session_tui.Dashboard(self.screen, self.config, self.env).refresher.close()
        self.assertEqual(timeouts, [int(session_refresh.FAST_SECONDS * 1000)])
        dash.refresher.close()


class FakeFeed:
    def __init__(self, results):
        self.results = list(results)

    def poll(self):
        return self.results.pop(0) if self.results else None

    def close(self):
        pass


if __name__ == '__main__':
    unittest.main()
