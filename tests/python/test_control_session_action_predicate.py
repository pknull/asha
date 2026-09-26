"""#102 phase 2 T6: the dashboard offers an action only when the hub command would take it.

Rows come from ``Hub.show()`` on real fixture sessions, never hand-simplified
rows (QA7 recommendation). Each offer is compared with the command's own
predicate and then with the command's actual outcome. The guard is proven to
discriminate by injecting the QA7 P2 view predicate, which re-derived
eligibility from presented activity, and watching it fail.
"""
import unittest
from unittest import mock

from lib.control import session_keys, session_order, session_tui
from lib.control.session_completion import no_handoff_eligibility
from lib.control.store import StoreError
from tests.python.test_session_no_handoff_close import Fixture

OFFER = 'c close (no handoff)'


def qa7_view_predicate(row, *, enabled):
    """The QA7 P2 defect: eligibility read from the presented (rewritten) activity."""
    return bool(enabled) and row.get('native_activity') == 'idle' and row.get('activity') in {'idle', 'closing'}


def idle(f, harness='claude'):
    sid = f.launch(harness=harness)['session_id']
    with f.acting_as(sid):
        f.hub.observe('prompt-submitted')
        f.hub.observe('turn-stopped')
    return sid


def close_pending(f):
    sid = idle(f)
    f.hub.close(sid)
    return sid


def qa7_working(f):
    sid = close_pending(f)
    with f.acting_as(sid):
        f.hub.report(state='working', body='Working without a new native hook')
    return sid


def report_outstanding(f):
    sid = close_pending(f)
    path = session_order.counter_path(f.config, sid, 1)
    path.write_text(f"{f.hub.get(sid)['event_order']['applied'] + 1}\n")
    return sid


def needs_input(f):
    sid = idle(f)
    with f.acting_as(sid):
        f.hub.report(state='needs-input', body='Which option?')
    return sid


QA7 = 'QA7: explicit working after a close request'
STATES = [('idle, close pending', close_pending), (QA7, qa7_working),
          ('a hook report outstanding', report_outstanding), ('needs input', needs_input),
          ('launched, never idle', lambda f: f.launch()['session_id']),
          ('idle, no close request', idle), ('codex idle', lambda f: idle(f, 'codex'))]


class ActionPredicateTests(unittest.TestCase):
    def each_state(self):
        """Yield (name, fixture, session id); the fake tmux models one pane, so one fixture per state."""
        for name, build in STATES:
            fixture = Fixture()
            fixture.setUp()
            try:
                yield name, fixture, build(fixture)
            finally:
                fixture.doCleanups()

    def offered(self, shown):
        text = session_tui.footer(shown, width=400, no_handoff_close=True)
        rendered = '\n'.join(session_tui.lines({'rows': [shown], 'no_handoff_close': True}, width=200, height=30))
        self.assertEqual(OFFER in text, OFFER in rendered)
        return OFFER in text

    def mismatches(self):
        """States where the offer disagrees with the command predicate or the command's outcome."""
        wrong = []
        for name, f, sid in self.each_state():
            shown = f.hub.show(sid)
            stored = f.hub.get(sid)
            counters = session_order.read_counters(f.config, stored, wait=0.05)
            predicate_ok = no_handoff_eligibility(stored, counters) is None
            offered = self.offered(shown)
            try:
                f.hub.close(sid, no_handoff=True)
                closed = True
            except StoreError:
                closed = False
            if not offered == predicate_ok == closed:
                wrong.append((name, offered, predicate_ok, closed))
        return wrong

    def test_every_offer_matches_the_command_predicate_and_outcome(self):
        self.assertEqual(self.mismatches(), [])

    def test_the_states_cover_both_verdicts(self):
        verdicts = {name: self.offered(f.hub.show(sid)) for name, f, sid in self.each_state()}
        self.assertEqual(set(verdicts.values()), {True, False}, verdicts)
        self.assertTrue(verdicts['idle, close pending'])
        self.assertFalse(verdicts[QA7])

    def test_the_guard_discriminates_the_qa7_view_predicate(self):
        with mock.patch.object(session_keys, 'offers_no_handoff', qa7_view_predicate):
            wrong = self.mismatches()
        self.assertIn(QA7, [name for name, *_ in wrong])

    def test_the_footer_reads_only_the_hub_verdict(self):
        # Presented activity that looks closable never produces the offer by itself.
        shown = {'session_id': 'x', 'activity': 'closing', 'native_activity': 'idle', 'group': 'current',
                 'next_step': 'Close: attach or --no-handoff', 'transport': 'terminal',
                 'no_handoff': {'eligible': False, 'reason': 'working'}}
        self.assertNotIn(OFFER, session_tui.footer(shown, width=400, no_handoff_close=True))
        self.assertIn(OFFER, session_tui.footer(dict(shown, no_handoff={'eligible': True}), width=400,
                                                no_handoff_close=True))
        self.assertNotIn(OFFER, session_tui.footer(dict(shown, no_handoff={'eligible': True}), width=400,
                                                   no_handoff_close=False))


def force_closed(f):
    sid = idle(f)
    f.hub.stop(sid, close=True)
    return sid


def stopped(f):
    sid = idle(f)
    f.hub.stop(sid)
    return sid


ENTER_STATES = [('idle, live', idle), ('launched, live', lambda f: f.launch()['session_id']),
                ('needs input, live', needs_input), ('close pending, live', close_pending),
                ('force-closed', force_closed), ('stopped', stopped)]


def always_attachable(row):
    """The pre-Q14-F5 view: Enter offered on every row, whatever the hub would do."""
    return True


class EnterAffordanceTests(unittest.TestCase):
    """Q14-F5: Enter is offered only where ``Hub.attach`` accepts the row (T6 extended)."""

    def each_state(self):
        for name, build in ENTER_STATES:
            fixture = Fixture()
            fixture.setUp()
            try:
                yield name, fixture, build(fixture)
            finally:
                fixture.doCleanups()

    def offered(self, shown):
        keys = session_keys.row_keys(shown, no_handoff_close=True)
        rendered = '\n'.join(session_tui.lines({'rows': [shown], 'no_handoff_close': True}, width=200, height=30))
        offer = any(key.startswith('Enter ') for key in keys)
        self.assertEqual(offer, 'Enter ' in rendered.splitlines()[-1], rendered)
        return offer

    def mismatches(self):
        wrong = []
        for name, f, sid in self.each_state():
            offered = self.offered(f.hub.show(sid))
            try:
                f.hub.attach(sid)
                accepted = True
            except StoreError:
                accepted = False
            if offered != accepted:
                wrong.append((name, offered, accepted))
        return wrong

    def test_enter_is_offered_exactly_where_attach_accepts(self):
        self.assertEqual(self.mismatches(), [])

    def test_the_states_cover_both_verdicts(self):
        verdicts = {name: self.offered(f.hub.show(sid)) for name, f, sid in self.each_state()}
        self.assertEqual(set(verdicts.values()), {True, False}, verdicts)
        self.assertFalse(verdicts['force-closed'])
        self.assertFalse(verdicts['stopped'])

    def test_the_guard_discriminates_an_always_offered_enter(self):
        with mock.patch.object(session_keys, 'attachable', always_attachable):
            wrong = self.mismatches()
        self.assertEqual(sorted(name for name, *_ in wrong), ['force-closed', 'stopped'])

    def test_a_history_row_offers_resume_but_no_unavailable_view(self):
        for name, f, sid in self.each_state():
            if name == 'force-closed':
                keys = session_keys.row_keys(f.hub.show(sid))
                self.assertIn('r resume', keys)
                self.assertFalse([key for key in keys if key.startswith('Enter')], keys)

    def test_structured_rows_keep_their_conversation_view(self):
        # Hub.attach accepts a structured row in any lifecycle: it opens the conversation.
        for lifecycle, group in (('closed', 'history'), ('open', 'current'), ('stopped', 'ended')):
            row = {'session_id': 'x', 'transport': 'structured', 'lifecycle': lifecycle, 'group': group,
                   'activity': 'idle', 'next_step': 'Idle'}
            self.assertTrue(any(k.startswith('Enter') for k in session_keys.row_keys(row)), lifecycle)


if __name__ == '__main__':
    unittest.main()
