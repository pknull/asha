"""Issue #101 QA8: one invariant for every turnless termination.

Ported from the QA8 probes (/tmp/aq9597/qa8). The real control-event.sh
allocates numbers from the real counter file; a capture endpoint decides whether
its report reaches the fixture hub. Old, lost or unsequenced orders are
constructed explicitly: the fixture otherwise allocates in call order.
Controller fixtures only, not native Claude/Codex proof.
"""
import contextlib
import fcntl
import io
import json
import os
import signal
import stat
import subprocess
import time
from pathlib import Path
from unittest import mock

from tests.python.test_control_session_closure import ClosureFixture
from tests.python import test_session_completion
from lib.control import hub_cli, session_order
from lib.control.store import StoreError

ROOT = Path(__file__).resolve().parents[2]
HOOK = ROOT / 'plugins/session/hooks/handlers/control-event.sh'


class OrderFixture(ClosureFixture):
    """The turnless close is experimental (#103): these fixtures opt in."""
    save = test_session_completion.CompletionTests.save

    def setUp(self):
        super().setUp()
        self.configure_control(no_handoff_close=True)

    def idle(self, harness='claude', receipt=False):
        sid = self.launch(harness=harness)['session_id']
        with self.acting_as(sid):
            self.hub.observe('prompt-submitted')
            if receipt:
                self.save(sid)
            self.hub.observe('turn-stopped')
        return sid

    def n(self, sid):
        return self.hub.get(sid)['event_order']['applied'] + 1

    def counter(self, sid):
        return session_order.counter_path(self.config, sid, self.hub.get(sid)['generation'])

    def protected(self, sid, no_handoff=True, wait=0):
        try:
            self.hub.close(sid, no_handoff=no_handoff, wait=wait)
        except StoreError:
            pass
        self.assertEqual(self.tmux.killed, [], 'a turnless close killed a live session')

    def hook_env(self, sid):
        fake = self.root / 'fake-hook-root'
        (fake / 'bin').mkdir(parents=True, exist_ok=True)
        script = fake / 'bin/asha'
        script.write_text('#!/usr/bin/env python3\nimport json,os,sys\n'
                          'with open(os.environ["QA_CAPTURE"],"w") as f: json.dump(sys.argv[1:],f)\nprint("{}")\n')
        script.chmod(0o700)
        cap = self.root / ('capture-' + str(time.monotonic_ns()))
        env = dict(os.environ, ASHA_ROOT=str(fake), ASHA_HUB_SESSION_ID=sid,
                   ASHA_HUB_GENERATION=str(self.hub.get(sid)['generation']),
                   ASHA_HUB_EVENT_ORDER=str(self.counter(sid)), QA_CAPTURE=str(cap), ASHA_ROOM_INPUT_FENCE='0')
        return env, cap

    def hook(self, sid, event='Stop'):
        """Run the real hook; its report is captured, never applied (a lost report)."""
        env, cap = self.hook_env(sid)
        done = subprocess.run(['bash', str(HOOK), event], input='{}', text=True, capture_output=True,
                              env=env, timeout=10)
        self.assertEqual(done.returncode, 0, done.stderr)
        args = json.loads(cap.read_text())
        return int(args[args.index('--order') + 1]) if '--order' in args else None


class LostReportTests(OrderFixture):
    """F1: a number allocated but never applied blocks every turnless kill."""

    def test_pending_or_lost_hook_tail_prevents_kill(self):
        for harness in ('claude', 'codex'):
            for receipt in (False, True):
                for no_handoff in (False, True):
                    with self.subTest(harness=harness, receipt=receipt, no_handoff=no_handoff):
                        self.tmux.killed.clear()
                        sid = self.idle(harness, receipt)
                        n = self.n(sid)
                        self.assertEqual(self.hook(sid, 'PreToolUse'), n)
                        self.protected(sid, no_handoff=no_handoff)
                        if no_handoff:
                            self.assertRegex(self.hub.show(sid)['no_handoff']['reason'], 'not arrived')
                        self.hub.stop(sid)
                        self.tmux.killed.clear()

    def test_crash_immediately_after_increment_prevents_kill(self):
        sid = self.idle()
        n = self.n(sid)
        env, cap = self.hook_env(sid)
        process = subprocess.Popen(['bash', str(HOOK), 'UserPromptSubmit'], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, start_new_session=True)
        deadline = time.monotonic() + 5
        while self.counter(sid).read_text().strip() != str(n):
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.001)
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate(timeout=5)
        self.assertFalse(cap.exists())
        self.protected(sid)

    def test_counter_lock_is_held_through_the_kill(self):
        sid = self.idle()
        seen = []
        original = self.hub._stop
        def stop(row, **kwargs):
            # A hook starting now cannot take a number: it reports unsequenced.
            seen.append(self.hook(sid, 'UserPromptSubmit'))
            return original(row, **kwargs)
        with mock.patch.object(self.hub, '_stop', side_effect=stop):
            self.assertEqual(self.hub.close(sid, no_handoff=True)['closure']['state'], 'closed-no-save-claimed')
        self.assertEqual(seen, [None])


class ReceiptOrderTests(OrderFixture):
    """F2: the receipt close obeys the same invariant, and ignored work still invalidates."""

    def test_receipt_gap_must_refuse_both_close_paths(self):
        for harness in ('claude', 'codex'):
            for no_handoff in (False, True):
                with self.subTest(harness=harness, no_handoff=no_handoff):
                    self.tmux.killed.clear()
                    sid = self.idle(harness, receipt=True)
                    n = self.n(sid)
                    with self.acting_as(sid):
                        self.hub.observe('turn-stopped', order=n + 1)
                    self.assertFalse(self.hub.show(sid)['no_handoff']['eligible'])
                    self.assertNotEqual(self.hub.show(sid)['completion_readiness']['receipt'], 'current')
                    self.protected(sid, no_handoff=no_handoff)
                    self.hub.stop(sid)
                    self.tmux.killed.clear()

    def test_receipt_unsequenced_old_stop_background(self):
        sid = self.idle(receipt=True)
        with self.acting_as(sid):
            self.hub.observe('turn-stopped', background_tasks=2)
            self.hub.observe('turn-stopped', order=None)
        for no_handoff in (False, True):
            self.protected(sid, no_handoff=no_handoff)

    def test_receipt_late_work_must_invalidate(self):
        sid = self.idle(receipt=True)
        n = self.n(sid)
        epoch = self.hub.get(sid)['work_epoch']
        with self.acting_as(sid):
            self.hub.observe('turn-stopped', order=n + 1)
            self.hub.observe('tool-started', tool_token='lost-work', order=n)
            self.hub.observe('turn-stopped', order=n + 2)
        shown = self.hub.show(sid)
        self.assertNotEqual(shown['work_epoch'], epoch)
        self.assertEqual(shown['completion_readiness']['receipt'], 'stale')
        result = self.hub.close(sid, no_handoff=True)
        self.assertEqual(result['closure']['state'], 'closed-no-save-claimed')

    def test_late_report_tool_events_after_receipt_do_not_invalidate_it(self):
        sid = self.idle(receipt=True)
        m = self.n(sid)
        receipt = self.hub.get(sid)['completion']
        with self.acting_as(sid):
            self.hub.observe('turn-stopped', order=m + 1)
            self.hub.observe('tool-completed', tool_kind='report', tool_token='r', order=m)   # late, not work
        self.assertEqual(self.hub.get(sid)['work_epoch'], receipt['work_epoch'])
        with self.acting_as(sid):
            self.hub.observe('turn-stopped', order=m + 2)                                   # after the evidence
        self.assertEqual(self.hub.show(sid)['completion_readiness']['receipt'], 'current')
        self.assertEqual(self.hub.close(sid)['closure']['state'], 'completed')

    def test_receipt_close_completes_on_a_clean_ordered_boundary(self):
        for harness in ('claude', 'codex'):
            sid = self.idle(harness, receipt=True)
            self.assertEqual(self.hub.show(sid)['completion_readiness']['receipt'], 'current')
            self.assertEqual(self.hub.close(sid)['closure']['state'], 'completed')
            self.tmux.killed.clear()


class CausalBarrierTests(OrderFixture):
    """F3: only a Stop allocated after unsequenced evidence clears it."""

    def test_unsequenced_new_work_cannot_be_cleared_by_older_numbered_stop(self):
        sid = self.idle()
        n = self.n(sid)
        old_order = self.hook(sid, 'Stop')
        with self.counter(sid).open('r+') as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            new_order = self.hook(sid, 'PreToolUse')
        self.assertEqual((old_order, new_order), (n, None))
        with self.acting_as(sid):
            self.hub.observe('tool-started', tool_token='new-work', order=None)
            self.hub.observe('turn-stopped', order=old_order)
        self.assertIsNotNone(self.hub.get(sid)['event_order']['barrier'])
        self.protected(sid)
        with self.acting_as(sid):
            self.hub.observe('prompt-submitted')
            self.hub.observe('turn-stopped')
        self.assertEqual(self.hub.close(sid, no_handoff=True)['closure']['state'], 'closed-no-save-claimed')

    def test_missing_or_corrupt_counter_never_self_repairs_and_resume_recovers(self):
        sid = self.idle()
        path = self.counter(sid)
        for bad in ('missing', '', 'garbage\n', '999999999\n', '4\ngarbage\n'):
            with self.subTest(bad=bad):
                if bad == 'missing':
                    path.unlink(missing_ok=True)
                else:
                    path.write_text(bad)
                    path.chmod(0o600)
                for _ in range(2):
                    order = self.hook(sid)
                    self.assertIsNone(order)
                    with self.acting_as(sid):
                        self.hub.observe('turn-stopped', order=None)
                if bad != 'missing':
                    self.assertEqual(path.read_text(), bad)
                self.protected(sid)
        self.hub.stop(sid)
        self.hub.resume(sid, prompt='New generation')
        self.assertEqual(self.counter(sid).read_text(), '0\n')
        order = self.hook(sid)
        with self.acting_as(sid):
            self.hub.observe('turn-stopped', order=order)
        self.assertIsNone(session_order.kill_refusal(
            self.hub.get(sid), session_order.read_counters(self.config, self.hub.get(sid))))


class CounterHardeningTests(OrderFixture):
    def test_creation_permissions_and_existing_file_validation(self):
        sid = self.launch()['session_id']
        path = self.counter(sid)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        for directory in (path.parent, path.parent.parent):
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        self.assertEqual(session_order.create_counter(self.config, sid, 1), str(path))
        target = path.with_name('target')
        target.write_text('9\n')
        path.unlink()
        path.symlink_to(target)
        with self.assertRaises(StoreError):
            session_order.create_counter(self.config, sid, 1)
        self.assertIsNone(self.hook(sid))
        path.unlink()
        path.write_text('7\n')
        path.chmod(0o666)
        with self.assertRaises(StoreError):
            session_order.create_counter(self.config, sid, 1)
        self.assertIsNone(self.hook(sid))
        self.assertIsNone(session_order.read_allocated(self.config, self.hub.get(sid)))


    def test_hook_attempt_limit_matches_the_hub(self):
        import re
        found = re.findall(r'^HUB_ATTEMPT_LIMIT=([0-9]+)$', HOOK.read_text(), re.M)
        self.assertEqual(found, [str(session_order.ATTEMPT_LIMIT)])


class LateStopCliTests(OrderFixture):
    """F4: an ignored Stop has no delivery or confirmation effects."""

    def test_duplicate_stop_must_not_decline_delivered_handoff(self):
        sid = self.launch()['session_id']
        with self.acting_as(sid):
            self.hub.observe('prompt-submitted', order=1)
            self.hub.observe('turn-stopped', order=2)
        self.hub.close(sid)
        with self.acting_as(sid), mock.patch.object(hub_cli, 'Hub', return_value=self.hub):
            self.counter(sid).write_text('3\n')
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(hub_cli.dispatch(['event', '--event', 'turn-stopped', '--order', '3'],
                                                  env=self.env), 0)
            self.assertIn('close request', out.getvalue())
            self.assertEqual(self.hub.get(sid)['closure']['state'], 'delivered')
            self.hub.observe('prompt-submitted', order=4)
            self.hub.observe('tool-started', tool_token='handoff-work', order=5)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(hub_cli.dispatch(['event', '--event', 'turn-stopped', '--order', '3'],
                                                  env=self.env), 0)
        self.assertEqual(out.getvalue().strip(), '{}')
        after = self.hub.get(sid)
        self.assertEqual(after['closure']['state'], 'delivered')
        self.assertEqual(after['active_tools'], {'handoff-work': 'work'})
        self.assertEqual(after['observation_log'][-1]['ignored'], 'late')


class UnallocatedAttemptTests(OrderFixture):
    """QA9 Q9-F1: a hook that could not take a number is still recorded as an attempt."""

    def contended_hook(self, sid, event='PreToolUse'):
        """The unchanged hook under ordinary lock contention: allocation times out."""
        before = self.counter(sid).read_text()
        with self.counter(sid).open('r+') as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            order = self.hook(sid, event)
        self.assertIsNone(order)
        self.assertEqual(self.counter(sid).read_text(), before)

    def test_hook_fallback_before_close_must_not_kill(self):
        for harness in ('claude', 'codex'):
            for receipt in (False, True):
                for no_handoff in ([False, True] if receipt else [True]):
                    with self.subTest(harness=harness, receipt=receipt, no_handoff=no_handoff):
                        sid = self.idle(harness, receipt)
                        self.contended_hook(sid)           # its report is lost
                        self.protected(sid, no_handoff=no_handoff)
                        self.assertRegex(self.hub.show(sid)['no_handoff']['reason'], 'started')
                        self.hub.stop(sid)
                        self.tmux.killed.clear()

    def test_a_stop_allocated_after_the_attempt_reconciles_it(self):
        sid = self.idle()
        self.contended_hook(sid)
        self.protected(sid)
        with self.acting_as(sid):
            self.hub.observe('prompt-submitted')
            self.hub.observe('turn-stopped')
        self.assertEqual(self.hub.close(sid, no_handoff=True)['closure']['state'], 'closed-no-save-claimed')

    def test_numbered_and_observed_fallback_controls(self):
        for mode in ('numbered_lost', 'unsequenced_observed'):
            with self.subTest(mode=mode):
                sid = self.idle(receipt=True)
                if mode == 'numbered_lost':
                    self.assertIsNotNone(self.hook(sid, 'PreToolUse'))
                else:
                    self.contended_hook(sid)
                    with self.acting_as(sid):
                        self.hub.observe('tool-started', tool_token='now-visible', order=None)
                self.protected(sid)
                self.hub.stop(sid)
                self.tmux.killed.clear()

    def test_real_cli_budget_loses_unallocated_hook(self):
        """The unchanged hook and event CLI, with the kill-decision lock pair held (QA9 cli-timeout)."""
        for harness in ('claude', 'codex'):
            for receipt in (False, True):
                with self.subTest(harness=harness, receipt=receipt):
                    sid = self.idle(harness, receipt)
                    env, cap = self.hook_env(sid)
                    env['QA_TIP'] = str(ROOT)
                    env['QA_ENV'] = json.dumps(self.env)
                    (Path(env['ASHA_ROOT']) / 'bin/asha').write_text(
                        '#!/usr/bin/env python3\nimport json,os,sys\nfrom pathlib import Path\n'
                        "sys.path.insert(0,os.environ['QA_TIP'])\n"
                        'from lib.control import hub_cli\nfrom lib.control.session_hub import Hub\n'
                        'from lib.control.config import load_config\n'
                        "env=json.loads(os.environ['QA_ENV'])\nhub=Hub(load_config(env),env=env)\n"
                        "hub.actor=lambda:hub.get(os.environ['ASHA_HUB_SESSION_ID'])\n"
                        'hub_cli.Hub=lambda *a,**k:hub\n'
                        "Path(os.environ['QA_CAPTURE']).write_text(json.dumps(sys.argv[1:]))\n"
                        'rc=hub_cli.dispatch(sys.argv[3:],env=env)\n'
                        "Path(os.environ['QA_CAPTURE']+'.returned').write_text(str(rc))\n")
                    before = self.counter(sid).read_text()
                    with self.hub._observation_lock(sid), self.counter(sid).open('r+') as held:
                        fcntl.flock(held, fcntl.LOCK_EX)
                        done = subprocess.run(['bash', str(HOOK), 'PreToolUse'], input='{}', text=True,
                                              capture_output=True, env=env, timeout=10)
                    self.assertEqual(done.returncode, 0)
                    self.assertNotIn('--order', json.loads(cap.read_text()))
                    self.assertFalse(Path(str(cap) + '.returned').exists())
                    self.assertEqual(self.counter(sid).read_text(), before)
                    self.protected(sid)
                    self.hub.stop(sid)
                    self.tmux.killed.clear()

    def test_attempt_log_is_private_append_only_and_bounded(self):
        sid = self.idle()
        attempts = session_order.attempts_path(self.config, sid, 1)
        self.assertEqual(stat.S_IMODE(attempts.stat().st_mode), 0o600)
        size = attempts.stat().st_size
        self.hook(sid)
        self.assertEqual(attempts.stat().st_size, size + 1)
        attempts.chmod(0o644)
        self.assertIsNone(self.hook(sid))
        self.assertEqual(attempts.stat().st_size, size + 1)          # refused before appending
        attempts.chmod(0o600)
        with mock.patch.object(session_order, 'ATTEMPT_LIMIT', size + 1):
            self.assertRegex(session_order.kill_refusal(
                self.hub.get(sid), session_order.read_counters(self.config, self.hub.get(sid))), 'full')


class StopAttemptSampleTests(OrderFixture):
    """QA10 Q10-F1: a Stop's attempt count is read before its number, never after.

    Ported from /tmp/aq9597/qa10/attempt-race.py: the Stop is paused once it
    holds its number, a later hook appends and loses its report, then the Stop
    reports. The count must not cover the later hook. Red before the fix (the
    size was read after the increment); attempt-race-control.py, where the
    later hook appends only after the size read, stays the negative control.
    """

    def paused_stop(self, sid):
        """Run the real Stop hook and pause it at its first external command after its number.

        Before the fix that is the size read (stat); after it, the event CLI.
        Scheduling gates only: the wrapper runs the real stat, and the endpoint
        only captures the report, as hook_env's does.
        """
        env, cap = self.hook_env(sid)
        gate = self.root / ('gate-' + sid)
        (gate / 'bin').mkdir(parents=True)
        release, ready = gate / 'release', gate / 'ready'
        os.mkfifo(release)
        pause = 'touch "$QA_READY"; read -r _ <"$QA_RELEASE"'
        wrapper = gate / 'stat'
        wrapper.write_text('#!/bin/bash\n'
                           'if [[ "$1" == -c && "$2" == %s && "$4" == "$QA_COUNTER.attempts"'
                           ' && "$(cat "$QA_COUNTER")" == "$QA_EXPECT" ]]; then ' + pause + '; fi\n'
                           'exec /usr/bin/stat "$@"\n')
        endpoint = gate / 'bin/asha'
        # Captured before pausing: the hook's controller budget may kill it.
        endpoint.write_text('#!/bin/bash\nprintf \'%s\\0\' "$@" >"$QA_CAPTURE.raw"\n' + pause + '\n')
        wrapper.chmod(0o700)
        endpoint.chmod(0o700)
        before = int(self.counter(sid).read_text())
        env.update(PATH=f"{gate}:{env['PATH']}", ASHA_ROOT=str(gate), QA_COUNTER=str(self.counter(sid)),
                   QA_EXPECT=str(before + 1), QA_READY=str(ready), QA_RELEASE=str(release))
        proc = subprocess.Popen(['bash', str(HOOK), 'Stop'], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, env=env)
        deadline = time.monotonic() + 5
        while not ready.exists():
            self.assertIsNone(proc.poll(), 'the Stop hook exited before its gate')
            self.assertLess(time.monotonic(), deadline, 'the Stop hook never reached its gate')
            time.sleep(0.005)
        self.assertEqual(int(self.counter(sid).read_text()), before + 1)   # the Stop has its number
        return proc, release, cap

    def test_later_lost_hook_is_never_covered_by_an_older_stop(self):
        for harness in ('claude', 'codex'):
            for receipt in (False, True):
                for no_handoff in ([False, True] if receipt else [True]):
                    with self.subTest(harness=harness, receipt=receipt, no_handoff=no_handoff):
                        sid = self.idle(harness, receipt)
                        proc, release, cap = self.paused_stop(sid)
                        try:
                            self.hook(sid, 'PreToolUse')     # appends after the Stop started; report lost
                        finally:
                            with release.open('w') as gate:
                                gate.write('go\n')
                            _, stderr = proc.communicate(timeout=5)
                        self.assertEqual(proc.returncode, 0, stderr)
                        args = Path(str(cap) + '.raw').read_text().split('\0')[:-1]
                        self.assertIn('--order', args)
                        with mock.patch.object(self.hub, 'actor', side_effect=lambda: self.hub.get(sid)), \
                                mock.patch.object(hub_cli, 'Hub', return_value=self.hub), \
                                contextlib.redirect_stdout(io.StringIO()):
                            self.assertEqual(hub_cli.dispatch(args[2:], env=self.env), 0)
                        self.assertEqual(self.hub.get(sid)['event_order']['last_event'], 'turn-stopped')
                        self.protected(sid, no_handoff=no_handoff)
                        # A Stop numbered after the lost hook reconciles it.
                        with self.acting_as(sid):
                            self.hub.observe('prompt-submitted')
                            self.hub.observe('turn-stopped')
                        self.assertEqual(self.hub.close(sid, no_handoff=True)['closure']['state'],
                                         'closed-no-save-claimed')
                        self.tmux.killed.clear()

    def test_a_hook_without_a_number_appends_twice(self):
        sid = self.idle()
        attempts = session_order.attempts_path(self.config, sid, 1)
        size = attempts.stat().st_size
        with self.counter(sid).open('r+') as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            self.assertIsNone(self.hook(sid, 'PreToolUse'))
        self.assertEqual(attempts.stat().st_size, size + 2)
        self.assertIsNotNone(self.hook(sid, 'PreToolUse'))
        self.assertEqual(attempts.stat().st_size, size + 3)


class OrderRecoveryTests(OrderFixture):
    """QA9 Q9-F2/F3: transient failures and overflow do not poison a whole incarnation."""

    def test_transient_counter_read_failure_recovers_within_the_generation(self):
        sid = self.idle()
        with self.counter(sid).open('r+') as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            with self.acting_as(sid):
                self.hub.observe('turn-stopped', order=None)       # the hub cannot read the counter now
        self.assertTrue(self.hub.get(sid)['event_order']['barrier_pending'])
        self.protected(sid)
        self.assertRegex(self.hub.show(sid)['no_handoff']['reason'], 'unreadable')
        with self.acting_as(sid):
            self.hub.observe('prompt-submitted')                    # resolves the barrier to the counter now
            self.hub.observe('turn-stopped')                        # allocated after it: clears
        row = self.hub.get(sid)
        self.assertFalse(row['event_order'].get('barrier_pending'))
        self.assertIsNone(row['event_order']['barrier'])
        self.assertEqual(self.hub.close(sid, no_handoff=True)['closure']['state'], 'closed-no-save-claimed')

    def test_missing_report_backlog_does_not_poison_a_later_receipt(self):
        sid = self.idle()
        old = self.hub.get(sid)['event_order']['applied']
        with self.acting_as(sid):
            self.hub.observe('turn-stopped', order=old + 66)          # 65 reports missing
        self.assertLessEqual(len(self.hub.get(sid)['event_order']['missing']), session_order.MISSING_LIMIT)
        with self.acting_as(sid):
            self.hub.observe('prompt-submitted')
            self.save(sid)
            self.hub.observe('turn-stopped')
        shown = self.hub.show(sid)
        self.assertIsNone(shown['event_order']['barrier'])
        self.assertEqual(shown['completion_readiness']['receipt'], 'current')
        self.assertEqual(self.hub.close(sid)['closure']['state'], 'completed')

    def test_forged_huge_order_is_bounded(self):
        sid = self.idle()
        with self.acting_as(sid):
            self.hub.observe('turn-stopped', order=session_order.ORDER_LIMIT - 1)
        self.assertLessEqual(len(self.hub.get(sid)['event_order']['missing']), session_order.MISSING_LIMIT)
        self.assertEqual(self.hub.get(sid)['event_order']['missing_dropped'],
                         session_order.ORDER_LIMIT - 2 - session_order.MISSING_LIMIT)

    def test_dropped_reports_after_a_receipt_still_refuse_it(self):
        """The overflow fix only forgives drops older than the receipt (pure, no fixture)."""
        row = dict(generation=1, event_order=dict(generation=1, applied=10, last_event='turn-stopped',
                                                  missing=[], barrier=None, attempts=10))
        receipt = dict(order_applied=10, generation=1)
        counters = session_order.Counters(80, 80)
        gap = session_order.MISSING_LIMIT + 6                     # 11..80 skipped, 11..16 dropped
        _, fields = session_order.observe(row, 'turn-stopped', 11 + gap, allocated=lambda: 80, attempts=80)
        row['event_order'] = fields['event_order']
        for late in row['event_order']['missing']:                # every kept report arrives late
            _, fields = session_order.observe(row, 'tool-started', late, allocated=lambda: 80)
            row['event_order'] = fields['event_order']
        self.assertEqual(row['event_order']['missing'], [])
        self.assertEqual(row['event_order']['missing_dropped'], 16)
        self.assertRegex(session_order.receipt_unaccounted(row, receipt, counters) or '', 'not arrived')
        self.assertIsNone(session_order.receipt_unaccounted(row, dict(receipt, order_applied=16), counters))
