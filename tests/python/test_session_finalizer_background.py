"""Issue #104: a standalone finalizer after background work and lost or late tool ends.

Controller fixtures, not native Claude delivery proof. The shapes come from the
native transcripts of the three blocked workers (09beeacf, 231da09e, d181a7ad):
every backgrounded Bash got its PostToolUse when it moved to the background;
what stayed in ``active_tools`` was a start whose end never reached the hub
(a failed tool on an install without the PostToolUseFailure hook, or a lost
hook report).
"""
import json
import os
import subprocess
import time
import uuid
from unittest import mock

from tests.python.test_control_session_closure import ClosureFixture
from lib.control import session_order
from lib.control.store import StoreError

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HOOK = ROOT / 'plugins/session/hooks/handlers/control-event.sh'


class FinalizerFixture(ClosureFixture):
    def tool(self, sid, start=None, end=None, kind='work'):
        """One tool call; ``start``/``end`` are explicit orders, or None to allocate in call order."""
        token = str(uuid.uuid4())
        self.hub.observe('tool-started', tool_kind=kind, tool_token=token,
                         **({} if start is None else {'order': start}))
        if end is not False:
            self.hub.observe('tool-completed', tool_kind=kind, tool_token=token,
                             **({} if end is None else {'order': end}))
        return token

    def applied(self, sid):
        return self.hub.get(sid)['event_order']['applied']

    def finalize(self):
        return self.hub.handoff(None, outcome='no-durable-update', detail='Nothing durable changed')['completion']


class BackgroundFinalizerTests(FinalizerFixture):
    def test_issue_scenario_background_shells_then_standalone_finalizer_is_ready(self):
        """The #104 scenario: backgrounded Bash, then a standalone handoff."""
        sid = self.launch()['session_id']
        with self.acting_as(sid):
            self.hub.observe('prompt-submitted')
            self.tool(sid)                       # run_in_background: PostToolUse arrives at once
            self.tool(sid)                       # a 600 s timeout moves it to the background: PostToolUse then
            self.hub.observe('turn-stopped', background_tasks=2)
            # The shells finish; Claude wakes on task notifications (no hook) and continues.
            self.tool(sid)
            receipt = self.finalize()
            self.assertEqual(receipt['status'], 'ready', receipt['detail'])
            self.assertTrue(self.hub.get(sid)['completion']['tool_finished'])
            self.hub.report(state='finished', body='Done')
            self.hub.observe('turn-stopped')
        self.assertEqual(self.hub.close(sid)['closure']['state'], 'completed')

    def test_background_turn_with_a_late_tool_end_is_ready(self):
        """d181a7ad: a tool end reported out of order still ends that tool."""
        sid = self.launch()['session_id']
        with self.acting_as(sid):
            self.hub.observe('prompt-submitted')
            self.tool(sid)
            self.hub.observe('turn-stopped', background_tasks=1)
            n = self.applied(sid) + 1
            late = self.tool(sid, start=n, end=False)          # its end takes n+1 but reports late
            self.tool(sid, start=n + 2, end=n + 3)
            self.assertEqual(self.hub.get(sid)['event_order']['missing'], [n + 1])
            self.hub.observe('tool-completed', tool_kind='work', tool_token=late, order=n + 1)
            self.assertEqual(self.hub.get(sid)['active_tools'], {})
            receipt = self.finalize()
            self.assertEqual(receipt['status'], 'ready', receipt['detail'])
            self.hub.report(state='finished', body='Done')
            self.hub.observe('turn-stopped')
        self.assertEqual(self.hub.close(sid)['closure']['state'], 'completed')

    def test_late_end_of_the_finalizer_itself_finishes_the_receipt(self):
        sid = self.launch()['session_id']
        with self.acting_as(sid):
            self.hub.observe('prompt-submitted')
            n = self.applied(sid) + 1
            final = str(uuid.uuid4())
            self.hub.observe('tool-started', tool_kind='finalizer', tool_token=final, order=n)
            # The class method: acting_as wraps the instance's handoff in its own finalizer.
            receipt = type(self.hub).handoff(self.hub, None, outcome='no-durable-update', detail='x')['completion']
            self.assertEqual(receipt['status'], 'ready', receipt['detail'])
            self.tool(sid, start=n + 2, end=n + 3, kind='report')
            self.assertFalse(self.hub.get(sid)['completion']['tool_finished'])
            self.hub.observe('tool-completed', tool_kind='finalizer', tool_token=final, order=n + 1)
            row = self.hub.get(sid)
            self.assertEqual(row['active_tools'], {})
            self.assertTrue(row['completion']['tool_finished'])
            self.assertEqual(row['completion']['status'], 'ready')


class GuaranteeTests(FinalizerFixture):
    """The fix retires ended tools only; it never makes an unended tool look ended."""

    def test_a_start_without_any_end_still_blocks_and_names_the_cause(self):
        sid = self.launch()['session_id']
        with self.acting_as(sid):
            self.hub.observe('prompt-submitted')
            self.tool(sid, end=False)                   # failed tool without PostToolUseFailure, or lost report
            receipt = self.finalize()
            self.assertEqual(receipt['status'], 'blocked')
            self.assertIsNone(receipt['tool_token'])
            self.assertRegex(receipt['detail'], r'1 other tool start .*no observed end')
            self.assertRegex(receipt['detail'], 'PostToolUseFailure')

    def test_a_duplicate_report_is_not_end_evidence(self):
        sid = self.launch()['session_id']
        with self.acting_as(sid):
            self.hub.observe('prompt-submitted')
            n = self.applied(sid) + 1
            running = self.tool(sid, start=n, end=False)
            # Same number as the start: a duplicate, not a report that filled a gap.
            self.hub.observe('tool-completed', tool_kind='work', tool_token=running, order=n)
            self.assertEqual(self.hub.get(sid)['active_tools'], {running: 'work'})
            self.assertEqual(self.finalize()['status'], 'blocked')

    def test_late_end_after_the_finalizer_started_stays_blocked(self):
        """Parallel work: its end is numbered after the finalizer's start."""
        sid = self.launch()['session_id']
        with self.acting_as(sid):
            self.hub.observe('prompt-submitted')
            n = self.applied(sid) + 1
            parallel = self.tool(sid, start=n, end=False)
            self.assertEqual(self.finalize()['status'], 'blocked')     # finalizer start n+1, end n+2
            self.hub.observe('tool-completed', tool_kind='work', tool_token=parallel, order=n + 3)
            row = self.hub.get(sid)
            self.assertEqual(row['completion']['status'], 'blocked')
            self.assertEqual(row['active_tools'], {})

class EndIdentityTests(FinalizerFixture):
    """QA19: a start is retired only by an end of THAT tool: same session, same native
    conversation, same token and kind, numbered after its start."""

    def test_q19_f1_foreign_conversation_late_end_never_retires(self):
        """Q19-F1 reproduction, both harness rows: the late branch validates native identity first."""
        for harness in ('claude', 'codex'):
            with self.subTest(harness=harness):
                sid = self.launch(harness=harness)['session_id']
                with self.acting_as(sid):
                    self.hub.observe('session-start', native_id='native-A', cwd=str(self.project))
                    self.hub.observe('tool-started', tool_token='running-A', tool_kind='work')
                    n = self.applied(sid)
                    self.hub.observe('tool-started', tool_token='report', tool_kind='report', order=n + 2)
                    self.hub.observe('tool-completed', tool_token='report', tool_kind='report', order=n + 3)
                    for order in (n + 4, n + 1):             # on time, then filling the gap
                        with self.assertRaisesRegex(StoreError, 'another native conversation'):
                            self.hub.observe('tool-completed', tool_token='running-A', tool_kind='work',
                                             native_id='native-B', cwd='/other-project', order=order)
                    self.assertEqual(self.hub.get(sid)['active_tools'], {'running-A': 'work'})
                    self.assertEqual(self.finalize()['status'], 'blocked')
                self.hub.stop(sid)

    def test_end_from_another_conversation_than_its_start_never_retires(self):
        """A start bound to A is not ended by an end naming no conversation, or one after a rebind."""
        sid = self.launch()['session_id']
        with self.acting_as(sid):
            self.hub.observe('session-start', native_id='native-A', cwd=str(self.project))
            self.hub.observe('tool-started', tool_token='t', tool_kind='work', native_id='native-A')
            n = self.applied(sid)
            self.hub.observe('turn-stopped', order=n + 2, native_id='native-A')
            self.hub.observe('prompt-submitted', order=n + 3, native_id='native-A')
            self.hub.observe('tool-started', tool_token='t2', tool_kind='work', order=n + 4, native_id='native-A')
            self.hub.observe('tool-completed', tool_token='t2', tool_kind='work', order=n + 1)   # no native id
            self.assertIn('t2', self.hub.get(sid)['active_tools'])
            # /clear rebinds to C inside the project; A's start is not ended by C's end.
            self.hub.observe('session-start', native_id='native-C', cwd=str(self.project))
            self.hub.observe('tool-completed', tool_token='t2', tool_kind='work', native_id='native-C')
            self.assertIn('t2', self.hub.get(sid)['active_tools'])

    def test_q19_f2_end_numbered_before_a_reused_start_never_retires_it(self):
        """Q19-F2 reproduction, every end kind: an end before the start is an older call's."""
        sid = self.launch()['session_id']
        with self.acting_as(sid):
            self.hub.observe('prompt-submitted')
            for kind in ('work', 'report', 'finalizer'):
                with self.subTest(kind=kind):
                    n = self.applied(sid)
                    token = 'reused-' + kind
                    self.hub.observe('tool-started', tool_token=token, tool_kind='work', order=n + 2)
                    self.hub.observe('tool-completed', tool_token=token, tool_kind=kind, order=n + 1)
                    self.assertEqual(self.hub.get(sid)['active_tools'].get(token), 'work')
            self.assertEqual(self.finalize()['status'], 'blocked')

    def test_late_end_of_another_kind_never_retires(self):
        sid = self.launch()['session_id']
        with self.acting_as(sid):
            self.hub.observe('prompt-submitted')
            n = self.applied(sid)
            self.hub.observe('tool-started', tool_token='t', tool_kind='work', order=n + 1)
            self.hub.observe('tool-started', tool_token='x', tool_kind='report', order=n + 3)
            self.hub.observe('tool-completed', tool_token='t', tool_kind='finalizer', order=n + 2)
            self.assertIn('t', self.hub.get(sid)['active_tools'])
            self.hub.observe('tool-completed', tool_token='t', tool_kind='work', order=n + 4)
            self.assertNotIn('t', self.hub.get(sid)['active_tools'])

    def test_unnumbered_start_or_end_in_an_ordered_session_proves_no_order(self):
        """Without both numbers the end is not proven to follow its start: stays open until Stop."""
        sid = self.launch()['session_id']
        with self.acting_as(sid):
            self.hub.observe('prompt-submitted')
            self.hub.observe('tool-started', tool_token='a', tool_kind='work', order=None)
            self.hub.observe('tool-completed', tool_token='a', tool_kind='work')
            self.hub.observe('tool-started', tool_token='b', tool_kind='work')
            self.hub.observe('tool-completed', tool_token='b', tool_kind='work', order=None)
            self.assertEqual(self.hub.get(sid)['active_tools'], {'a': 'work', 'b': 'work'})
            self.assertEqual(self.finalize()['status'], 'blocked')
            self.hub.observe('turn-stopped')
            self.assertEqual(self.hub.get(sid)['active_tools'], {})

    def test_an_unknown_token_end_retires_nothing(self):
        """A truncated or unclassified payload names no tool: its end is no evidence."""
        sid = self.launch()['session_id']
        with mock.patch.object(self.hub, 'actor', side_effect=lambda: self.hub.get(sid)):
            self.hub.observe('prompt-submitted')
            self.hub.observe('tool-started', tool_token='unknown', tool_kind='work')
            self.hub.observe('tool-completed', tool_token='unknown', tool_kind='work')
            self.assertEqual(self.hub.get(sid)['active_tools'], {'unknown': 'work'})

    def test_legacy_unordered_session_keeps_arrival_order(self):
        """No event counter at all (structured or pre-#101 sessions): arrival order, as before."""
        sid = self.launch()['session_id']
        with mock.patch.object(self.hub, 'actor', side_effect=lambda: self.hub.get(sid)):
            self.hub.observe('prompt-submitted')
            self.hub.observe('tool-started', tool_token='a', tool_kind='work')
            self.hub.observe('tool-completed', tool_token='a', tool_kind='work')
            self.assertEqual(self.hub.get(sid)['active_tools'], {})
            self.assertEqual(self.hub.get(sid)['tool_starts'], {})


class LostEndReportTests(FinalizerFixture):
    """A tool end whose hook report fails is lost: nothing spools, retries or replays it
    (the spool was removed after QA20/QA22). Its start stays open until the turn's Stop,
    and the blocked receipt names lost reports as a cause."""

    def fake_root(self, behaviour):
        fake = self.root / ('fake-root-' + str(time.monotonic_ns()))
        (fake / 'bin').mkdir(parents=True)
        calls = fake / 'calls'
        script = fake / 'bin/asha'
        script.write_text(
            '#!/usr/bin/env python3\nimport json,os,sys,time\n'
            'd=os.environ["QA_CALLS"]\nos.makedirs(d,exist_ok=True)\nn=len(os.listdir(d))\n'
            'open(os.path.join(d,str(n)),"w").write(json.dumps(sys.argv[1:]))\n'
            'b=os.environ["QA_BEHAVIOUR"]\n'
            'time.sleep(2 if b=="hang" else 0)\n'
            'sys.exit(1 if b=="fail" else 0)\n')
        script.chmod(0o700)
        return fake, calls

    def run_hook(self, sid, event, behaviour, *, native='native-A', tool_use_id='toolu_1'):
        fake, calls = self.fake_root(behaviour)
        generation = self.hub.get(sid)['generation']
        env = dict(os.environ, ASHA_ROOT=str(fake), ASHA_HUB_SESSION_ID=sid, ASHA_HUB_GENERATION=str(generation),
                   QA_CALLS=str(calls), QA_BEHAVIOUR=behaviour, ASHA_ROOM_INPUT_FENCE='0',
                   ASHA_HUB_EVENT_ORDER=str(session_order.counter_path(self.config, sid, generation)))
        payload = json.dumps({'tool_name': 'Bash', 'tool_input': {'command': 'false'}, 'tool_use_id': tool_use_id,
                              'session_id': native, 'cwd': str(self.project)})
        started = time.monotonic()
        done = subprocess.run(['bash', str(HOOK), event], input=payload, text=True, capture_output=True,
                              env=env, timeout=10)
        return done, time.monotonic() - started, calls

    def order_dir(self, sid):
        return session_order.counter_path(self.config, sid, self.hub.get(sid)['generation']).parent

    def test_spool_and_replay_are_absent(self):
        self.assertFalse(hasattr(session_order, 'take_lost_ends'))
        self.assertFalse(hasattr(self.hub, 'replay_lost_ends'))
        self.assertNotIn('.end.', HOOK.read_text())

    def test_failed_end_report_writes_nothing_and_is_not_retried(self):
        sid = self.launch()['session_id']
        for event, behaviour in (('PostToolUse', 'fail'), ('PostToolUseFailure', 'hang')):
            with self.subTest(event=event, behaviour=behaviour):
                before = sorted(p.name for p in self.order_dir(sid).iterdir())
                done, elapsed, calls = self.run_hook(sid, event, behaviour)
                self.assertEqual((done.returncode, done.stdout.strip()), (0, '{}'), done.stderr)
                self.assertLess(elapsed, 1.5)
                time.sleep(1.0)
                self.assertEqual(len(os.listdir(calls)), 1, 'a report ran after the hook returned')
                self.assertEqual(sorted(p.name for p in self.order_dir(sid).iterdir()), before)

    def test_lost_end_blocks_names_the_cause_and_clears_at_stop(self):
        sid = self.launch()['session_id']
        with self.acting_as(sid):
            self.hub.observe('session-start', native_id='native-A', cwd=str(self.project))
            self.hub.observe('prompt-submitted', native_id='native-A')
            from completion_event import metadata
            kind, token = metadata({'tool_name': 'Bash', 'tool_input': {'command': 'false'},
                                    'tool_use_id': 'toolu_1'})
            self.hub.observe('tool-started', tool_kind=kind, tool_token=token, native_id='native-A')
        done, _, _ = self.run_hook(sid, 'PostToolUse', 'fail')       # the end report is lost
        self.assertEqual(done.returncode, 0, done.stderr)
        with self.acting_as(sid):
            receipt = self.finalize()
            self.assertEqual(receipt['status'], 'blocked')
            self.assertRegex(receipt['detail'], 'lost end report')
            self.assertIn(token, self.hub.get(sid)['active_tools'])
            self.hub.observe('turn-stopped', native_id='native-A')
            self.assertEqual(self.hub.get(sid)['active_tools'], {})
            self.assertEqual(self.hub.get(sid)['tool_starts'], {})

    def test_q22_planted_order_directory_entries_are_never_read_or_removed(self):
        """QA20 F1/F3 and QA22 F1/F3/F4 reproductions have no reader left: planted files,
        hard links to outside files, FIFOs and symlinks survive a handoff untouched."""
        sid = self.launch()['session_id']
        directory = self.order_dir(sid)
        outside = self.root / 'outside-private'
        outside.write_text(json.dumps(dict(tool_kind='work', tool_token='t', native_id=None, cwd=None,
                                           order=7, attempts=None)))
        outside.chmod(0o600)
        (directory / '1.end.7').write_text(outside.read_text())
        (directory / '1.end.7').chmod(0o600)
        os.link(outside, directory / '1.end.8')
        os.mkfifo(directory / '1.end.9', 0o600)
        (directory / '1.end.10').symlink_to(outside)
        with self.acting_as(sid):
            self.hub.observe('prompt-submitted')
            self.hub.observe('tool-started', tool_kind='work', tool_token='t', order=5)
            started = time.monotonic()
            self.assertEqual(self.finalize()['status'], 'blocked')
            self.assertLess(time.monotonic() - started, 5)
            self.assertEqual(self.hub.get(sid)['active_tools'], {'t': 'work'})
        self.assertEqual(sorted(p.name for p in directory.iterdir() if '.end.' in p.name),
                         ['1.end.10', '1.end.7', '1.end.8', '1.end.9'])
        self.assertEqual(outside.stat().st_nlink, 2)
        self.assertEqual((directory / '1.end.7').read_text(), outside.read_text())


class LegacyOpenToolTests(FinalizerFixture):
    """A tool left open by the revision before #104 has no ``tool_starts`` entry. There is
    no migration: no end retires it (its start's conversation and order are unknown, QA22 F2),
    and it clears at the turn's Stop like any other unmatched start."""

    def pre_update(self, sid, active, starts=()):
        """The row as the parent revision left it: open tools, no start metadata (key absent by default)."""
        row = dict(self.hub.get(sid), active_tools=dict(active))
        if starts == ():
            row.pop('tool_starts', None)
        else:
            row['tool_starts'] = starts
        with self.hub.database() as db, db.transaction(write=True) as c:
            self.hub._save(c, row)

    def test_adoption_is_absent(self):
        from lib.control import session_completion
        self.assertFalse(hasattr(session_completion, 'adopt_legacy_starts'))

    def test_q20_f2_unordered_legacy_open_tool_stays_open_until_stop(self):
        """QA20 reproduction, now by design: unordered session bound to A, retained {'old': 'work'}."""
        sid = self.launch()['session_id']
        with mock.patch.object(self.hub, 'actor', side_effect=lambda: self.hub.get(sid)):
            self.hub.observe('session-start', native_id='A', cwd=str(self.project))
            for starts in ((), {}, None):
                with self.subTest(tool_starts=starts):
                    self.pre_update(sid, {'old': 'work'}, starts)
                    self.hub.observe('tool-completed', tool_token='old', native_id='A')
                    self.hub.observe('tool-completed', tool_token='old')
                    self.assertEqual(self.hub.get(sid)['active_tools'], {'old': 'work'})
                    self.assertNotIn('old', self.hub.get(sid).get('tool_starts') or {})
            self.hub.observe('turn-stopped', native_id='A')
            self.assertEqual(self.hub.get(sid)['active_tools'], {})

    def test_ordered_legacy_open_tool_blocks_until_stop_then_the_handoff_is_ready(self):
        sid = self.launch()['session_id']
        with self.acting_as(sid):
            self.hub.observe('session-start', native_id='A', cwd=str(self.project))
            self.hub.observe('prompt-submitted', native_id='A')
            self.hub.observe('tool-started', tool_kind='work', tool_token='old', native_id='A')
            self.pre_update(sid, {'old': 'work'})
            self.hub.observe('tool-completed', tool_kind='work', tool_token='old', native_id='A')
            self.assertIn('old', self.hub.get(sid)['active_tools'])
            self.assertEqual(self.finalize()['status'], 'blocked')
            self.hub.observe('turn-stopped', native_id='A')
            self.assertEqual(self.hub.get(sid)['active_tools'], {})
            self.hub.observe('prompt-submitted', native_id='A')
            receipt = self.finalize()
            self.assertEqual(receipt['status'], 'ready', receipt['detail'])

    def test_q22_f2_rebind_before_upgrade_never_retires_the_old_conversations_tool(self):
        """QA22 reproduction: A starts 'shared-token', the parent rebinds to B, then B ends that token."""
        sid = self.launch()['session_id']
        with self.acting_as(sid):
            self.hub.observe('session-start', native_id='A', cwd=str(self.project))
            self.hub.observe('prompt-submitted', native_id='A')
            self.hub.observe('session-start', native_id='B', cwd=str(self.project))
            self.pre_update(sid, {'shared-token': 'work'})
            self.hub.observe('tool-completed', tool_kind='work', tool_token='shared-token', native_id='B')
            self.assertIn('shared-token', self.hub.get(sid)['active_tools'])


# A command the default destructive-git rule denies (built here so the test
# source never carries it literally).
DENIED = ' '.join(('git', 'reset', '--hard'))


class DeniedToolTests(ClosureFixture):
    """Q19-F3/F5: a guard's denial is not verifiable end evidence, so no guard reports one.
    A denied call's start stays open until the turn's Stop and the blocked detail says so."""

    GUARD = ROOT / 'plugins/session/hooks/handlers/policy-guard.sh'
    SECRETS = ROOT / 'plugins/session/hooks/handlers/block-secrets.sh'

    def guard_env(self, sid, calls):
        fake = self.root / ('fake-root-' + str(time.monotonic_ns()))
        (fake / 'bin').mkdir(parents=True)
        script = fake / 'bin/asha'
        script.write_text('#!/usr/bin/env python3\nimport json,os,sys,time\n'
                          'd=os.environ["QA_CALLS"]\nos.makedirs(d,exist_ok=True)\n'
                          'open(os.path.join(d,str(time.monotonic_ns())),"w").write(json.dumps(sys.argv[1:]))\n')
        script.chmod(0o700)
        env = {k: v for k, v in os.environ.items() if not k.startswith('ASHA_')}
        generation = self.hub.get(sid)['generation']
        env.update(ASHA_ROOT=str(fake), ASHA_HOME=str(self.root / 'asha-home'), ASHA_HARNESS='claude',
                   QA_CALLS=str(calls), ASHA_ROOM_INPUT_FENCE='0', ASHA_HUB_SESSION_ID=sid,
                   ASHA_HUB_GENERATION=str(generation),
                   ASHA_HUB_EVENT_ORDER=str(session_order.counter_path(self.config, sid, generation)))
        return env

    def test_q19_f3_tooldenied_is_not_a_bridge_mode(self):
        """Q19-F3 reproduction: an in-session `control-event.sh ToolDenied` reports nothing."""
        sid = self.launch()['session_id']
        calls = self.root / 'forged-calls'
        payload = json.dumps({'tool_name': 'Bash', 'tool_use_id': 'toolu_sleep', 'tool_input': {'command': 'sleep 20'}})
        done = subprocess.run(['bash', str(HOOK), 'ToolDenied'], input=payload, text=True, capture_output=True,
                              env=self.guard_env(sid, calls), timeout=10)
        self.assertEqual((done.returncode, done.stdout.strip()), (0, '{}'))
        time.sleep(0.5)
        self.assertFalse(calls.exists() and os.listdir(calls))

    def test_q19_f5_guards_report_no_end_for_a_denied_call(self):
        """Q19-F5: no detached, timed end report exists to race the start's number."""
        sid = self.launch()['session_id']
        for guard, tool_input in ((self.GUARD, {'command': DENIED}), (self.SECRETS, {'file_path': '/p/.env'})):
            with self.subTest(guard=guard.name):
                calls = self.root / ('calls-' + str(time.monotonic_ns()))
                payload = json.dumps({'tool_name': 'Bash', 'tool_use_id': 'toolu_x', 'tool_input': tool_input})
                done = subprocess.run(['bash', str(guard)], input=payload, text=True, capture_output=True,
                                      env=self.guard_env(sid, calls), timeout=10)
                self.assertEqual(done.returncode, 2, done.stderr)
                time.sleep(1.0)
                self.assertFalse(calls.exists() and os.listdir(calls))

    def test_denied_start_blocks_until_stop_and_names_the_cause(self):
        sid = self.launch()['session_id']
        with self.acting_as(sid):
            self.hub.observe('prompt-submitted')
            self.hub.observe('tool-started', tool_kind='work', tool_token='denied')
            receipt = self.hub.handoff(None, outcome='no-durable-update', detail='x')['completion']
            self.assertEqual(receipt['status'], 'blocked')
            self.assertRegex(receipt['detail'], 'denied')
            self.hub.observe('turn-stopped')
            self.assertEqual(self.hub.get(sid)['active_tools'], {})
