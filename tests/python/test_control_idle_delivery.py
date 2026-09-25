"""Issue #96: close and send reach an idle terminal session without attachment.

Controller fixtures with a scripted tmux; not native delivery proof.
"""
import json
import time
from unittest import mock

from lib.control.config import load_config
from lib.control.session_hub import Hub
from lib.control.store import StoreError
from lib.control.tmux import RoomInputRefused
from tests.python.test_control_pane_input import CLAUDE_IDLE, CODEX_NATIVE_BLANK_FIRST, RULE
from tests.python.test_control_session_closure import ClosureFixture

CODEX_IDLE = ["", "\x1b[1m›\x1b[0m \x1b[2mImplement {feature}\x1b[0m", "", "  ? for shortcuts"]


def write_control(asha_home, **control):
    asha_home.mkdir(parents=True, exist_ok=True)
    asha_home.chmod(0o700)
    path = asha_home / 'config.json'
    path.write_text(json.dumps({'control': control}))
    path.chmod(0o600)


class IdleDeliveryEnabled(ClosureFixture):
    """Idle-pane typing is experimental: these fixtures opt in (control.idle_delivery)."""

    def setUp(self):
        super().setUp()
        write_control(self.asha_home, idle_delivery=True)
        self.config = load_config(self.env)
        self.assertTrue(self.config.idle_delivery)
        self.hub = Hub(self.config, env=self.env, tmux=self.tmux)


class IdleCloseDeliveryTests(IdleDeliveryEnabled):
    def idle(self, sid, *, harness='claude'):
        self.tmux.screen = list(CLAUDE_IDLE if harness == 'claude' else CODEX_IDLE)
        with self.acting_as(sid):
            self.hub.observe('prompt-submitted', native_id='native-' + sid[:8])
            self.hub.observe('turn-stopped')

    def test_idle_claude_close_is_typed_into_the_owned_pane_bound_to_its_request(self):
        sid = self.launch()['session_id']
        self.idle(sid)
        record = self.hub.close(sid)['closure']
        self.assertEqual(record['state'], 'delivered')
        self.assertEqual(record['delivery']['channel'], 'pane-injection')
        self.assertFalse(record.get('attachment_required'))
        self.assertEqual(len(self.tmux.injected), 1)
        pane, typed = self.tmux.injected[0]
        self.assertEqual(pane, self.tmux.pane_id)
        self.assertNotIn('\n', typed)
        self.assertIn(record['request_id'] + ' --attempt 1', typed)
        # Repeated close never types a second copy of the same attempt.
        self.assertEqual(self.hub.close(sid)['closure']['state'], 'delivered')
        self.assertEqual(len(self.tmux.injected), 1)
        with self.acting_as(sid):
            self.hub.observe('prompt-submitted')
            self.hub.handoff(record['request_id'], attempt=1, outcome='no-durable-update',
                             detail='Reviewed (nothing durable); digests unchanged.')
            self.hub.observe('turn-stopped')
        closed = self.hub.close(sid)
        self.assertEqual(closed['lifecycle'], 'closed')
        self.assertEqual(closed['closure']['state'], 'completed')

    def test_attached_pane_is_never_typed_into(self):
        sid = self.launch()['session_id']
        self.idle(sid)
        self.tmux.attached = 1
        result = self.hub.close(sid)
        self.assertEqual(self.tmux.injected, [])
        self.assertEqual(result['closure']['state'], 'pending-delivery')
        self.assertTrue(result['closure']['attachment_required'])
        self.assertIn('attached', result['closure']['last_error'])
        self.assertEqual(result['next_step'], 'Close needs attach')

    def test_non_empty_or_unproven_input_line_is_never_typed_into(self):
        for screen in ([RULE, '❯ half typed', RULE], []):
            with self.subTest(screen=screen):
                sid = self.launch()['session_id']
                self.idle(sid)
                self.tmux.screen = screen
                result = self.hub.close(sid)
                self.assertEqual(self.tmux.injected, [])
                self.assertTrue(result['closure']['attachment_required'])
                self.assertIn('input line', result['closure']['last_error'])
                self.hub.stop(sid)

    def test_working_session_waits_for_its_stop_boundary(self):
        sid = self.launch()['session_id']
        self.tmux.screen = list(CLAUDE_IDLE)
        with self.acting_as(sid):
            self.hub.observe('prompt-submitted')
        record = self.hub.close(sid)['closure']
        self.assertEqual(record['delivery']['channel'], 'stop-hook')
        self.assertEqual(self.tmux.injected, [])

    def test_detached_retry_injects_the_same_pending_attempt(self):
        sid = self.launch()['session_id']
        self.idle(sid)
        self.tmux.attached = 1
        first = self.hub.close(sid)['closure']
        self.tmux.attached = 0
        retried = self.hub.close(sid)['closure']
        self.assertEqual(retried['request_id'], first['request_id'])
        self.assertEqual(retried['attempts'], 1)
        self.assertEqual(retried['state'], 'delivered')
        self.assertEqual(len(self.tmux.injected), 1)

    def test_unanswered_injection_rearms_with_the_next_attempt(self):
        sid = self.launch()['session_id']
        self.idle(sid)
        rid = self.hub.close(sid)['closure']['request_id']
        with self.acting_as(sid):
            self.hub.observe('prompt-submitted')
            self.assertIsNone(self.hub.stop_decision(self.hub.observe('turn-stopped')))
        self.assertEqual(self.hub.show(sid)['closure']['state'], 'unanswered')
        rearmed = self.hub.close(sid)['closure']
        self.assertEqual(rearmed['attempts'], 2)
        self.assertEqual(rearmed['state'], 'delivered')
        self.assertIn(rid + ' --attempt 2', self.tmux.injected[-1][1])

    def test_old_idle_observation_still_permits_a_verified_injection(self):
        sid = self.launch()['session_id']
        self.idle(sid)
        self.hub._update(sid, native_observed_at=time.time() - 3600, observed_at=time.time() - 3600)
        result = self.hub.close(sid)
        self.assertEqual(result['closure']['state'], 'delivered')
        self.assertFalse(result['closure'].get('attachment_required'))
        self.assertNotEqual(result['next_step'], 'Close needs attach')

    def test_idle_codex_close_is_typed_without_restarting_the_room(self):
        sid = self.launch(harness='codex')['session_id']
        self.idle(sid, harness='codex')
        created = len(self.tmux.created)
        record = self.hub.close(sid)['closure']
        self.assertEqual(record['state'], 'delivered')
        self.assertEqual(record['delivery']['channel'], 'pane-injection')
        self.assertEqual(len(self.tmux.created), created)
        self.assertEqual(self.tmux.killed, [])

    def test_attached_codex_pane_is_neither_typed_into_nor_restarted(self):
        sid = self.launch(harness='codex')['session_id']
        self.idle(sid, harness='codex')
        self.tmux.attached = 1
        created = len(self.tmux.created)
        record = self.hub.close(sid)['closure']
        self.assertEqual(self.tmux.injected, [])
        self.assertEqual(self.tmux.killed, [])
        self.assertEqual(len(self.tmux.created), created)
        self.assertTrue(record['attachment_required'])

    def test_rearmed_request_requires_an_explicit_attempt(self):
        sid = self.launch()['session_id']
        self.idle(sid)
        rid = self.hub.close(sid)['closure']['request_id']
        with self.acting_as(sid):
            self.hub.observe('prompt-submitted')
            self.hub.stop_decision(self.hub.observe('turn-stopped'))
        self.assertEqual(self.hub.close(sid)['closure']['attempts'], 2)
        with self.acting_as(sid):
            self.hub.observe('prompt-submitted')
            with self.assertRaisesRegex(StoreError, '--attempt'):
                self.hub.handoff(rid, outcome='no-durable-update', detail='Reply without its attempt selector')
            with self.assertRaisesRegex(StoreError, 'stale close delivery attempt'):
                self.hub.handoff(rid, attempt=1, outcome='no-durable-update', detail='Reply to the first attempt')
            result = self.hub.handoff(rid, attempt=2, outcome='no-durable-update', detail='Reply to the current attempt')
        self.assertEqual(result['closure_state'], 'acknowledged')

    def test_copilot_and_opencode_are_never_typed_into(self):
        for harness in ('copilot', 'opencode'):
            with self.subTest(harness=harness):
                sid = self.launch(harness=harness)['session_id']
                self.tmux.screen = list(CLAUDE_IDLE)
                self.hub._update(sid, native_activity='idle', native_observed_at=time.time())
                self.hub.close(sid)
                self.assertEqual(self.tmux.injected, [])
                self.hub.stop(sid)


class InjectionRefusalTests(IdleCloseDeliveryTests):
    """QA #96: every refusal past the first probe keeps the Room and its person safe."""

    def assert_room_kept(self, sid, generation=1):
        row = self.hub.get(sid)
        self.assertEqual(self.tmux.killed, [])
        self.assertEqual(row['generation'], generation)
        self.assertTrue(row['closure']['attachment_required'])
        self.assertNotEqual(row['closure']['delivery']['channel'], 'native-resume')
        return row['closure']

    def test_codex_draft_after_a_blank_first_line_is_neither_typed_nor_restarted(self):
        for screen in (['› ', '  DO NOT SUBMIT THIS DRAFT', '', '  ? for shortcuts'], CODEX_NATIVE_BLANK_FIRST):
            with self.subTest(screen=screen):
                sid = self.launch(harness='codex')['session_id']
                self.idle(sid, harness='codex')
                self.tmux.screen = list(screen)
                self.hub.close(sid)
                self.assertEqual(self.tmux.injected, [])
                self.assertEqual(self.tmux.pasted, [])
                self.assert_room_kept(sid)
                self.hub.stop(sid)
                self.tmux.killed.clear()

    def test_attach_between_probe_and_paste_keeps_an_attached_codex_room(self):
        sid = self.launch(harness='codex')['session_id']
        self.idle(sid, harness='codex')
        self.tmux.before_paste = lambda: setattr(self.tmux, 'attached', 1)
        self.hub.close(sid)
        self.assertEqual(self.tmux.pasted, [])
        record = self.assert_room_kept(sid)
        self.assertEqual(record['input_refusal'], 'attached')

    def test_attach_between_paste_and_enter_is_a_partial_delivery_never_restarted(self):
        for harness in ('claude', 'codex'):
            with self.subTest(harness=harness):
                sid = self.launch(harness=harness)['session_id']
                self.idle(sid, harness=harness)
                self.tmux.before_enter = lambda: setattr(self.tmux, 'attached', 1)
                self.hub.close(sid)
                self.assertEqual(len(self.tmux.pasted), 1)
                self.assertEqual(self.tmux.injected, [])
                record = self.assert_room_kept(sid)
                self.assertEqual(record['input_refusal'], 'partial')
                self.assertEqual(record['state'], 'pending-delivery')
                # The typed text now occupies the input line: a later close never retypes it.
                self.tmux.attached = 0
                self.tmux.before_enter = None
                self.hub.close(sid)
                self.assertEqual(len(self.tmux.pasted), 1)
                self.assert_room_kept(sid)
                self.hub.stop(sid)
                self.tmux.killed.clear()
                self.tmux.pasted.clear()

    def test_attach_and_detach_cycle_after_capture_refuses_the_paste(self):
        # Any attach bumps the tmux attach generation, even within one second.
        sid = self.launch()['session_id']
        self.idle(sid)
        self.tmux.before_paste = lambda: setattr(self.tmux, 'attach_generation', '2')
        self.hub.close(sid)
        self.assertEqual(self.tmux.pasted, [])
        self.assertEqual(self.assert_room_kept(sid)['input_refusal'], 'attached')

    def test_attach_cycle_between_confirmation_and_enter_is_never_submitted(self):
        sid = self.launch()['session_id']
        self.idle(sid)
        self.tmux.before_enter = lambda: setattr(self.tmux, 'attach_generation', '2')
        self.hub.close(sid)
        self.assertEqual(len(self.tmux.pasted), 1)
        self.assertEqual(self.tmux.injected, [])
        self.assertEqual(self.assert_room_kept(sid)['input_refusal'], 'partial')

    def test_room_without_an_attach_generation_needs_attach_and_is_never_restarted(self):
        for harness in ('claude', 'codex'):
            with self.subTest(harness=harness):
                sid = self.launch(harness=harness)['session_id']
                self.idle(sid, harness=harness)
                self.tmux.attach_generation = None
                result = self.hub.close(sid)
                self.assertEqual(self.tmux.pasted, [])
                self.assertEqual(self.assert_room_kept(sid)['input_refusal'], 'unfenced')
                self.assertEqual(result['next_step'], 'Close needs attach')
                self.tmux.attach_generation = '0'
                self.hub.stop(sid)
                self.tmux.killed.clear()

    # QA2 #96 finding 3: after the paste the whole composer must hold exactly the text.
    def test_changed_composer_after_paste_is_never_submitted(self):
        def blank_line_draft(text):
            return ['\x1b[1m›\x1b[0m ' + text, '', '  DO NOT SUBMIT DRAFT', '', '  GPT-6-Astra xhigh']
        cases = (
            ('codex', blank_line_draft),
            ('claude', lambda text: [RULE, '❯ ' + text.replace('Asha Control', 'AshaControl'), RULE]),
            ('claude', lambda text: [RULE, '❯ [Pasted text #1 +10 lines]', RULE]),
        )
        for harness, screen in cases:
            with self.subTest(harness=harness, screen=screen('TEXT')):
                sid = self.launch(harness=harness)['session_id']
                self.idle(sid, harness=harness)
                self.tmux.after_paste = lambda screen=screen: setattr(
                    self.tmux, 'screen', screen(self.tmux.pasted[-1][1]))
                record = self.hub.close(sid)['closure']
                self.assertEqual(len(self.tmux.pasted), 1)
                self.assertEqual(self.tmux.injected, [])
                self.assertEqual(record['input_refusal'], 'partial')
                self.assert_room_kept(sid)
                self.tmux.after_paste = None
                self.hub.stop(sid)
                self.tmux.killed.clear()
                self.tmux.pasted.clear()

    # QA2/QA3 #96 finding 1: a native event that began is visible without any lock.
    def test_event_begun_at_the_paste_or_enter_seam_is_never_submitted(self):
        for seam, pasted, category in (('before_paste', 0, 'stale'), ('before_enter', 1, 'partial')):
            with self.subTest(seam=seam):
                sid = self.launch()['session_id']
                self.idle(sid)
                # The hook bumped the pane sequence; its hub report has not landed.
                setattr(self.tmux, seam, lambda: setattr(
                    self.tmux, 'event_sequence', str(int(self.tmux.event_sequence) + 1)))
                record = self.hub.close(sid)['closure']
                self.assertEqual(len(self.tmux.pasted), pasted)
                self.assertEqual(self.tmux.injected, [])
                self.assertEqual(record['input_refusal'], category)
                self.assert_room_kept(sid)
                setattr(self.tmux, seam, None)
                self.hub.stop(sid)
                self.tmux.killed.clear()
                self.tmux.pasted.clear()

    def test_lost_or_unsequenced_observation_refuses_until_a_sequenced_one_lands(self):
        sid = self.launch()['session_id']
        self.idle(sid)
        # A reporter killed after bumping the sequence: the hub never heard.
        self.tmux.event_sequence = str(int(self.tmux.event_sequence) + 1)
        record = self.hub.close(sid)['closure']
        self.assertEqual(self.tmux.pasted, [])
        self.assertEqual(record['input_refusal'], 'stale')
        self.assertIn('pending or its observation was lost', record['last_error'])
        with self.acting_as(sid):
            self.hub.observe('turn-stopped', sequence=None)  # a hook that could not sequence
        self.assertEqual(self.hub.close(sid)['closure']['input_refusal'], 'stale')
        with self.acting_as(sid):
            # A sequence bumped on some other pane never counts for this Room.
            self.hub.observe('turn-stopped', sequence=int(self.tmux.event_sequence), sequence_pane='%999')
        self.assertIsNone(self.hub.get(sid)['event_sequence'])
        self.assertEqual(self.hub.close(sid)['closure']['input_refusal'], 'stale')
        with self.acting_as(sid):
            self.hub.observe('turn-stopped')
        self.assertEqual(self.hub.close(sid)['closure']['state'], 'delivered')

    def test_recorded_sequence_from_an_earlier_room_pane_never_matches_a_new_one(self):
        sid = self.launch()['session_id']
        # A higher count left behind by a previous Room pane must not be kept by max().
        self.hub._update(sid, event_sequence=7, event_sequence_pane='%7')
        self.idle(sid)  # two sequenced events on this Room's pane: 1, 2
        row = self.hub.get(sid)
        self.assertEqual((row['event_sequence'], row['event_sequence_pane']), (2, self.tmux.pane_id))
        self.assertEqual(self.hub.close(sid)['closure']['state'], 'delivered')

    def test_native_hook_killed_at_its_budget_during_delivery_never_leaves_a_submission(self):
        """lock_probe (QA3): real control-event.sh, its real timeout, a 0.9 s transport stall."""
        import json, os, shlex, subprocess, sys
        from pathlib import Path
        # The fixture pane's event sequence lives in a file the hook's tmux call bumps.
        counter = self.root / 'pane-event-sequence'
        counter.write_text(self.tmux.event_sequence)
        sequence = property(lambda _self: counter.read_text().strip(),
                            lambda _self, value: counter.write_text(value))
        self.enterContext(mock.patch.object(type(self.tmux), 'event_sequence', sequence, create=True))
        for seam, slow in (('before_paste', False), ('before_enter', False), ('before_paste', True)):
            with self.subTest(seam=seam, reporter_killed=slow):
                sid = self.launch()['session_id']
                self.idle(sid)
                # The hook's tmux call lands on this counter (the fixture pane).
                fake_bin = self.root / f'fake-bin-{sid}'
                fake_bin.mkdir()
                (fake_bin / 'tmux').write_text(
                    '#!/bin/sh\nn=$(( $(cat ' + shlex.quote(str(counter)) + ') + 1 ))\n'
                    'printf %s "$n" > ' + shlex.quote(str(counter)) + '\necho "$n"\n')
                (fake_bin / 'tmux').chmod(0o700)
                done = self.root / f'done-{sid}'
                helper = self.root / f'event-{sid}.py'
                helper.write_text(
                    'import json, sys, time\nfrom pathlib import Path\n'
                    f'sys.path.insert(0, {str(Path.cwd())!r})\n'
                    'from lib.control.config import load_config\nfrom lib.control.session_hub import Hub\n'
                    'env = json.loads(Path(sys.argv[1]).read_text())\n'
                    'args = sys.argv[3:]\nsequence = int(args[args.index("--sequence") + 1]) if "--sequence" in args else None\n'
                    'pane = args[args.index("--sequence-pane") + 1] if "--sequence-pane" in args else None\n'
                    f'time.sleep({2.0 if slow else 0})\n'
                    'hub = Hub(load_config(env), env=env)\nhub.actor = lambda: hub.get(sys.argv[2])\n'
                    'hub.observe("prompt-submitted", sequence=sequence, sequence_pane=pane)\n'
                    f'Path({str(done)!r}).write_text("1")\n')
                config = self.root / f'env-{sid}.json'
                config.write_text(json.dumps(self.env))
                launcher_root = self.root / f'launcher-{sid}'
                (launcher_root / 'bin').mkdir(parents=True)
                launcher = launcher_root / 'bin' / 'asha'
                launcher.write_text('#!/bin/sh\nexec ' + shlex.join(
                    [sys.executable, str(helper), str(config), sid]) + ' "$@"\n')
                launcher.chmod(0o700)
                hook = Path('plugins/session/hooks/handlers/control-event.sh').resolve()
                outcome = {}

                def seam_call():
                    env = dict(os.environ, ASHA_ROOT=str(launcher_root), ASHA_HUB_SESSION_ID=sid,
                               ASHA_ROOM_INPUT_FENCE='1',
                               TMUX='/nonexistent/socket,1,0', TMUX_PANE=self.tmux.pane_id,
                               PATH=str(fake_bin) + os.pathsep + os.environ.get('PATH', ''))
                    process = subprocess.Popen(['bash', str(hook), 'UserPromptSubmit'], stdin=subprocess.PIPE,
                                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
                    process.stdin.write(b'{}')
                    process.stdin.close()
                    process.stdin = None
                    time.sleep(0.9)  # a transport stall longer than the hook's controller budget
                    outcome['stdout'] = process.communicate(timeout=5)[0].decode().strip()
                setattr(self.tmux, seam, seam_call)
                initial = self.hub.get(sid)
                record = self.hub.close(sid)['closure']
                self.assertEqual(outcome['stdout'], '{}')
                self.assertEqual(self.tmux.injected, [], 'no submission while a native event is unrecorded')
                self.assertNotEqual(record['state'], 'delivered')
                self.assertEqual(record['input_refusal'], 'stale' if seam == 'before_paste' else 'partial')
                row = self.hub.get(sid)
                if slow:
                    # The reporter was killed at its budget: nothing recorded, yet delivery refused.
                    self.assertFalse(done.exists())
                    self.assertEqual(row['native_activity'], 'idle')
                    self.assertEqual(self.hub.close(sid)['closure']['input_refusal'], 'stale')
                else:
                    # No lock to wait on: the observation landed within the hook budget.
                    self.assertTrue(done.exists())
                    self.assertEqual(row['native_activity'], 'working')
                    self.assertNotEqual(row['work_epoch'], initial['work_epoch'])
                setattr(self.tmux, seam, None)
                self.hub.stop(sid)
                self.tmux.killed.clear()
                self.tmux.pasted.clear()

    def test_mode_and_ownership_refusals_are_typed_and_never_restart(self):
        for category in ('mode', 'ownership'):
            with self.subTest(category=category):
                sid = self.launch(harness='codex')['session_id']
                self.idle(sid, harness='codex')
                def refuse(category=category):
                    raise RoomInputRefused(category, 'refused before paste; nothing was typed')
                self.tmux.before_paste = refuse
                self.hub.close(sid)
                self.assertEqual(self.assert_room_kept(sid)['input_refusal'], category)
                self.tmux.before_paste = None
                self.hub.stop(sid)
                self.tmux.killed.clear()

    def test_new_work_observed_after_the_screen_probe_is_never_typed_into(self):
        sid = self.launch()['session_id']
        self.idle(sid)
        original = self.tmux.room_input_facts
        def racing(pane):
            facts = original(pane)
            with self.acting_as(sid):
                self.hub.observe('prompt-submitted')
            self.tmux.screen = [RULE, '❯ DRAFT ENTERED AFTER CAPTURE', RULE]
            return facts
        self.tmux.room_input_facts = racing
        record = self.hub.close(sid)['closure']
        self.assertEqual(self.tmux.pasted, [])
        self.assertNotEqual(record['state'], 'delivered')

    def test_work_observed_after_paste_leaves_the_text_unsubmitted(self):
        sid = self.launch()['session_id']
        self.idle(sid)
        def work():
            with self.acting_as(sid):
                self.hub.observe('prompt-submitted')
        self.tmux.after_paste = work
        record = self.hub.close(sid)['closure']
        self.assertEqual(len(self.tmux.pasted), 1)
        self.assertEqual(self.tmux.injected, [])
        self.assertEqual(record['input_refusal'], 'partial')

    def test_draft_merged_into_the_paste_is_never_submitted(self):
        sid = self.launch()['session_id']
        self.idle(sid)
        self.tmux.paste_prefix = 'DRAFT TYPED BEFORE PASTE '
        record = self.hub.close(sid)['closure']
        self.assertEqual(len(self.tmux.pasted), 1)
        self.assertEqual(self.tmux.injected, [])
        self.assertEqual(record['input_refusal'], 'partial')
        self.assertEqual(self.tmux.killed, [])

    def test_restart_fallback_refuses_a_room_attached_at_kill_time(self):
        sid = self.launch(harness='codex')['session_id']
        self.idle(sid, harness='codex')
        self.tmux.screen = ['plain output without a composer']
        self.tmux.before_kill = lambda: setattr(self.tmux, 'attached', 1)
        self.hub.close(sid)
        record = self.assert_room_kept(sid)
        self.assertEqual(record['input_refusal'], 'attached')


class IdleSendDeliveryTests(IdleDeliveryEnabled):
    def test_send_to_an_idle_session_types_a_pointer_to_the_retained_message(self):
        sid = self.launch()['session_id']
        self.tmux.screen = list(CLAUDE_IDLE)
        with self.acting_as(sid):
            self.hub.observe('turn-stopped')
        sent = self.hub.send(sid, 'Please also check the README', key='readme')
        self.assertEqual(sent['delivery'], 'injected')
        self.assertEqual(sent['state'], 'queued')
        typed = self.tmux.injected[0][1]
        self.assertIn(sent['message_id'], typed)
        self.assertIn('ack-message ' + sent['message_id'], typed)
        # Idempotent retry of the same key does not type again.
        again = self.hub.send(sid, 'Please also check the README', key='readme')
        self.assertEqual(again['message_id'], sent['message_id'])
        self.assertEqual(len(self.tmux.injected), 1)

    def test_send_to_a_working_or_attached_session_only_queues(self):
        for setup in ('working', 'attached'):
            with self.subTest(setup=setup):
                sid = self.launch()['session_id']
                self.tmux.screen = list(CLAUDE_IDLE)
                with self.acting_as(sid):
                    self.hub.observe('turn-stopped' if setup == 'attached' else 'prompt-submitted')
                self.tmux.attached = 1 if setup == 'attached' else 0
                sent = self.hub.send(sid, 'Later', key='later-' + setup)
                self.assertEqual(sent['delivery'], 'queued-until-read')
                self.assertTrue(sent['delivery_detail'])
                self.assertEqual(self.tmux.injected, [])
                self.hub.stop(sid)
                self.tmux.attached = 0


class IdleDeliveryDisabledByDefaultTests(ClosureFixture):
    """Default configuration: close and send never read or type into a pane."""

    def idle(self, sid, *, harness='claude'):
        self.tmux.screen = list(CLAUDE_IDLE if harness == 'claude' else CODEX_IDLE)
        with self.acting_as(sid):
            self.hub.observe('prompt-submitted', native_id='native-' + sid[:8])
            self.hub.observe('turn-stopped')

    def no_pane_input(self):
        self.tmux.room_input_facts = mock.Mock(side_effect=AssertionError('pane read'))
        self.tmux.inject_owned_room_input = mock.Mock(side_effect=AssertionError('pane input'))

    def test_default_config_disables_idle_typing(self):
        self.assertFalse(load_config(self.env).idle_delivery)
        self.assertFalse(self.hub.config.idle_delivery)

    def test_idle_claude_close_needs_attach_without_touching_the_pane(self):
        sid = self.launch()['session_id']
        self.idle(sid)
        self.no_pane_input()
        result = self.hub.close(sid)
        self.assertEqual(result['closure']['state'], 'pending-delivery')
        self.assertEqual(result['closure']['delivery']['channel'], 'stop-hook')
        self.assertTrue(result['closure']['attachment_required'])
        self.assertEqual(result['closure']['input_refusal'], 'disabled')
        self.assertEqual(result['next_step'], 'Close needs attach')
        self.assertEqual((self.tmux.pasted, self.tmux.injected), ([], []))
        # A repeated close still types nothing.
        self.hub.close(sid)
        self.assertEqual(self.tmux.injected, [])

    def test_idle_codex_close_keeps_the_native_resume_fallback_and_never_types(self):
        sid = self.launch(harness='codex')['session_id']
        self.idle(sid, harness='codex')
        self.no_pane_input()
        record = self.hub.close(sid)['closure']
        self.assertEqual((self.tmux.pasted, self.tmux.injected), ([], []))
        self.assertEqual(record['delivery']['channel'], 'native-resume')

    def test_send_only_queues(self):
        sid = self.launch()['session_id']
        self.idle(sid)
        self.no_pane_input()
        sent = self.hub.send(sid, 'Please also check the README', key='readme')
        self.assertEqual(sent['delivery'], 'queued-until-read')
        self.assertIn('control.idle_delivery', sent['delivery_detail'])
        self.assertEqual(self.tmux.injected, [])

    def test_rooms_get_no_input_fence_unless_enabled(self):
        first = self.launch()['session_id']
        created, respawned = self.tmux.created[-1], self.tmux.respawned[-1][1]
        self.assertFalse(created.get('attach_fence'))
        # "0" overrides a server-global marker; the child also unsets it.
        self.assertEqual(created['environment']['ASHA_ROOM_INPUT_FENCE'], '0')
        self.assertIn('ASHA_ROOM_INPUT_FENCE', respawned[:respawned.index('--') if '--' in respawned else len(respawned)])
        self.assertEqual(respawned[respawned.index('ASHA_ROOM_INPUT_FENCE') - 1], '-u')
        # Retire the first Room: the fixture tmux models one Room identity at a time.
        self.hub.stop(first)
        write_control(self.asha_home, idle_delivery=True)
        self.hub = Hub(load_config(self.env), env=self.env, tmux=self.tmux)
        self.launch(name='Second room')
        created, respawned = self.tmux.created[-1], self.tmux.respawned[-1][1]
        self.assertTrue(created['attach_fence'])
        self.assertEqual(created['environment']['ASHA_ROOM_INPUT_FENCE'], '1')
        self.assertNotIn('ASHA_ROOM_INPUT_FENCE', respawned)

    def test_pending_close_guidance_names_typing_only_when_enabled(self):
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                write_control(self.asha_home, idle_delivery=enabled)
                self.hub = Hub(load_config(self.env), env=self.env, tmux=self.tmux)
                sid = self.launch(name=f'Guidance {enabled}')['session_id']
                with self.acting_as(sid):
                    self.hub.observe('prompt-submitted')
                self.hub.close(sid)
                guidance = self.hub.show(sid)['closure']['guidance']
                self.assertIn('when its current turn stops', guidance)
                self.assertEqual('typed into its pane' in guidance, enabled)
                self.hub.stop(sid)

    def test_rearmed_stop_hook_request_still_requires_its_attempt(self):
        sid = self.launch()['session_id']
        self.no_pane_input()
        with self.acting_as(sid):
            self.hub.observe('prompt-submitted')
            rid = self.hub.close(sid)['closure']['request_id']
            stopped = self.hub.observe('turn-stopped')
            decision = self.hub.stop_decision(stopped)
            self.hub.confirm_delivery(stopped, decision.receipt)
            self.hub.observe('prompt-submitted')
            self.hub.stop_decision(self.hub.observe('turn-stopped'))
        self.assertEqual(self.hub.show(sid)['closure']['state'], 'unanswered')
        self.assertEqual(self.hub.close(sid)['closure']['attempts'], 2)
        with self.acting_as(sid):
            self.hub.observe('prompt-submitted')
            with self.assertRaisesRegex(StoreError, '--attempt'):
                self.hub.handoff(rid, outcome='no-durable-update', detail='Reply without its attempt selector')
            result = self.hub.handoff(rid, attempt=2, outcome='no-durable-update', detail='Reply to the current attempt')
        self.assertEqual(result['closure_state'], 'acknowledged')
        self.assertEqual(self.tmux.injected, [])

    def test_invalid_setting_is_refused(self):
        write_control(self.asha_home, idle_delivery='yes')
        with self.assertRaisesRegex(ValueError, 'idle_delivery'):
            load_config(self.env)


class FinalizerEvidenceTests(ClosureFixture):
    def test_stop_clears_tool_starts_that_never_reported_an_end(self):
        sid = self.launch()['session_id']
        with mock.patch.object(self.hub, 'actor', side_effect=lambda: self.hub.get(sid)):
            self.hub.observe('prompt-submitted')
            self.hub.observe('tool-started', tool_kind='work', tool_token='failed-read')
            self.hub.observe('turn-stopped')
            self.assertEqual(self.hub.get(sid)['active_tools'], {})

    def test_handoff_after_failed_reads_in_a_stop_continuation_is_accepted(self):
        sid = self.launch()['session_id']
        with mock.patch.object(self.hub, 'actor', side_effect=lambda: self.hub.get(sid)):
            self.hub.observe('prompt-submitted')
            self.hub.observe('tool-started', tool_kind='work', tool_token='stale')
            rid = self.hub.close(sid)['closure']['request_id']
            stopped = self.hub.observe('turn-stopped')
            decision = self.hub.stop_decision(stopped)
            self.assertEqual(decision['decision'], 'block')
            self.hub.confirm_delivery(stopped, decision.receipt)
            self.hub.observe('tool-started', tool_kind='finalizer', tool_token='final')
            result = self.hub.handoff(rid, attempt=1, outcome='no-durable-update', detail='Nothing durable')
            self.assertEqual(result['completion']['status'], 'ready')
            self.assertEqual(result['closure_state'], 'acknowledged')

    def test_handoff_failed_status_names_the_cause_not_the_agents_outcome(self):
        sid = self.launch()['session_id']
        with mock.patch.object(self.hub, 'actor', side_effect=lambda: self.hub.get(sid)):
            rid = self.hub.close(sid)['closure']['request_id']
            self.hub.handoff(rid, attempt=1, outcome='no-durable-update', detail='Nothing durable')
        record = self.hub.show(sid)['closure']
        self.assertEqual(record['state'], 'handoff-failed')
        guidance = record['guidance']
        self.assertNotIn('project memory was not saved', guidance)
        self.assertIn('finalizer', guidance)
        self.assertIn('no-durable-update', guidance)

    def test_close_request_asks_for_the_handoff_as_its_own_tool_call(self):
        sid = self.launch()['session_id']
        self.hub.close(sid)
        body = self.hub.messages(sid)[0]['body']
        self.assertIn('only command in its own tool call', body)
