"""#149: an actor verb from another Claude conversation than the generation bound is refused.

Claude Code can move a Room's conversation to a background session under a new
native ID, and the background host keeps the first Room's environment. Claude
Code gives every Bash tool and hook process CLAUDE_CODE_SESSION_ID, the
session_id its hooks carry (changelog 2.1.132; 2.1.319 sets it last from the
running conversation's ID). A different value from the generation's binding is
positive evidence; an absent one, or no binding yet, keeps today's behaviour.
Controller fixtures, not native delivery proof.
"""
import contextlib
import io
import os
from unittest import mock

from lib.control.store import StoreError
from tests.python.test_control_session_closure import ClosureFixture

REFUSAL = 'another native conversation than this session bound'


class BoundConversationTests(ClosureFixture):
    def room(self, harness='claude', bound='conv-bound'):
        row = self.launch(harness=harness)
        sid = row['session_id']
        if bound:
            # The Room's own first hook event binds the generation (F6).
            self.actor(sid, conversation=bound).observe('prompt-submitted', native_id=bound,
                                                         cwd=str(self.project.resolve()))
        return sid

    def worker_env(self, sid, conversation=None, harness='claude'):
        env = dict(self.env, ASHA_HUB_SESSION_ID=sid, ASHA_HUB_GENERATION='1', ASHA_HARNESS=harness,
                   ASHA_SESSION_PROFILE='room')
        if conversation is not None:
            env['CLAUDE_CODE_SESSION_ID'] = conversation
        return env

    def actor(self, sid, conversation=None, harness='claude'):
        from lib.control.session_hub import Hub
        return Hub(self.config, env=self.worker_env(sid, conversation, harness), tmux=self.tmux)

    def queued(self, sid):
        self.hub.send(sid, 'Also check the footer', key='footer')
        return self.hub.messages(sid)[0]['message_id']

    def publications(self, sid):
        with self.hub.database() as db, db.transaction() as c:
            return c.execute('SELECT COUNT(*) FROM hub_memory_publications WHERE session_id=?', (sid,)).fetchone()[0]

    def cli(self, env, *argv):
        from lib.control import hub_cli
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = hub_cli.dispatch(list(argv), env=env)
        return code, err.getvalue()

    def verbs(self, hub, sid, mid):
        active, decisions = self.drafts()
        return {
            'report': lambda: hub.report(state='finished', body='Done as another Room'),
            'worker report': lambda: hub.observe(None, state='needs-input', body='Which project?'),
            'handoff --read': lambda: hub.handoff_read(),
            'handoff': lambda: hub.handoff(None, active_file=active, decisions_file=decisions,
                                           expected=self.digests()),
            'attestation': lambda: hub.handoff(None, outcome='no-durable-update', detail='nothing'),
            'ack-message': lambda: hub.acknowledge(mid),
        }

    def test_a_moved_conversation_cannot_act_as_the_room(self):
        sid = self.room()
        mid = self.queued(sid)
        before, memory = self.hub.get(sid), self.digests()
        moved = self.actor(sid, conversation='conv-moved')
        for label, verb in self.verbs(moved, sid, mid).items():
            with self.subTest(label), self.assertRaisesRegex(StoreError, REFUSAL):
                verb()
        after = self.hub.get(sid)
        self.assertEqual((after['report'], after['result'], after['current_assignment']),
                         (before['report'], before['result'], before['current_assignment']))
        self.assertEqual(self.publications(sid), 0)
        self.assertEqual(self.hub.messages(sid)[0]['state'], 'queued')
        self.assertEqual(self.digests(), memory)
        for argv in (['report', '--state', 'finished', '--text', 'Done'], ['handoff', '--read', '--json'],
                     ['handoff', '--outcome', 'no-durable-update', '--detail', 'nothing', '--json'],
                     ['messages', '--json'], ['ack-message', mid, '--json']):
            with self.subTest(argv[0]):
                code, err = self.cli(self.worker_env(sid, 'conv-moved'), *argv)
                self.assertEqual(code, 2)
                self.assertIn(REFUSAL, err)
        self.assertEqual(self.publications(sid), 0)

    def test_a_moved_conversation_cannot_explicitly_save_as_the_room(self):
        from lib.control.session_closure import memory_v2
        from tests.python.test_control_session_closure import ACTIVE, DECISIONS
        sid = self.room()
        memory = self.digests()
        with mock.patch.dict(os.environ, self.worker_env(sid, 'conv-moved'), clear=True), \
                self.assertRaisesRegex(StoreError, REFUSAL):
            memory_v2.publish(self.project, ACTIVE, DECISIONS)
        self.assertEqual((self.digests(), self.publications(sid)), (memory, 0))
        with mock.patch.dict(os.environ, self.worker_env(sid, 'conv-bound'), clear=True):
            receipt = memory_v2.publish(self.project, ACTIVE, DECISIONS)
        self.assertEqual((receipt['hub_publication_status'], self.publications(sid)), ('recorded', 1))

    def test_the_binding_decides_even_after_it_ended_or_the_row_moved_on(self):
        sid = self.room()
        rid = self.hub.request_close(sid, wait=60)['closure']['request_id']
        moved = self.actor(sid, conversation='conv-moved')
        with self.assertRaisesRegex(StoreError, REFUSAL):
            moved.handoff(rid, outcome='no-durable-update', detail='answering the close as the Room')
        # The row's last native_id is not the binding; the binding decides.
        with self.hub.database() as db, db.transaction(write=True) as c:
            current = self.hub.get(sid)
            current['native_id'] = 'conv-moved'
            self.hub._save(c, current)
        with self.assertRaisesRegex(StoreError, REFUSAL):
            moved.report(state='finished', body='Done as the Room')
        # An ended binding is still evidence until a SessionStart rebinds (F6).
        self.actor(sid, conversation='conv-bound').observe('session-ended', native_id='conv-bound')
        self.assertTrue(self.hub.get(sid)['native_binding'].get('ended'))
        with self.assertRaisesRegex(StoreError, REFUSAL):
            moved.handoff_read()
        self.assertEqual(self.publications(sid), 0)

    def test_the_bound_conversation_and_an_unknown_one_act_as_today(self):
        for conversation in ('conv-bound', None, ''):
            with self.subTest(conversation=conversation):
                sid = self.room()
                mid = self.queued(sid)
                hub = self.actor(sid, conversation=conversation)
                verbs = self.verbs(hub, sid, mid)
                self.assertTrue(verbs['handoff --read']()['memory']['available'])
                verbs['ack-message']()
                self.assertEqual(verbs['handoff']()['hub_publication_status'], 'recorded')
                self.assertEqual(verbs['report']()['report']['state'], 'finished')
                code, err = self.cli(self.worker_env(sid, conversation), 'messages', '--json')
                self.assertEqual((code, err), (0, ''))

    def test_no_binding_yet_is_no_evidence(self):
        # Hooks not reporting (#100) leave no binding: nothing to compare.
        sid = self.room(bound=None)
        self.assertIsNone(self.hub.get(sid).get('native_binding'))
        hub = self.actor(sid, conversation='conv-anything')
        self.assertTrue(hub.handoff_read()['memory']['available'])
        self.assertEqual(hub.report(state='finished', body='Done')['report']['state'], 'finished')

    def test_a_resumed_generation_compares_only_its_own_binding(self):
        sid = self.room()
        self.hub.stop(sid)
        self.hub.resume(sid, prompt='Continue')
        hub = self.actor(sid, conversation='conv-resumed')
        hub.env['ASHA_HUB_GENERATION'] = '2'
        self.assertEqual(hub.report(state='working')['report'], None)
        hub.observe('session-start', native_id='conv-resumed', cwd=str(self.project.resolve()), source='resume')
        hub.env['CLAUDE_CODE_SESSION_ID'] = 'conv-bound'
        with self.assertRaisesRegex(StoreError, REFUSAL):
            hub.report(state='finished', body='From the first generation')

    def test_hook_events_still_rebind_on_clear_and_the_new_conversation_acts(self):
        # A hook process carries CLAUDE_CODE_SESSION_ID equal to its payload's
        # session_id; the binding rules (F6) judge hook events, not this check.
        sid = self.room()
        cleared = self.actor(sid, conversation='conv-cleared')
        cleared.observe('session-start', native_id='conv-cleared', cwd=str(self.project.resolve()), source='clear')
        self.assertEqual(self.hub.get(sid)['native_binding']['native_id'], 'conv-cleared')
        self.assertEqual(cleared.report(state='finished', body='After /clear')['report']['state'], 'finished')
        with self.assertRaisesRegex(StoreError, REFUSAL):
            self.actor(sid, conversation='conv-bound').report(state='finished', body='The old conversation')

    def test_other_harnesses_ignore_an_inherited_claude_session_id(self):
        # A tmux server started from a Claude Bash tool hands its environment,
        # CLAUDE_CODE_SESSION_ID included, to every pane: another harness's
        # value is inherited, never its own.
        for harness in ('codex', 'copilot', 'opencode'):
            with self.subTest(harness=harness):
                sid = self.room(harness=harness, bound='thread-own')
                self.assertEqual(self.hub.get(sid)['native_binding']['native_id'], 'thread-own')
                hub = self.actor(sid, conversation='conv-of-the-chair', harness=harness)
                self.assertTrue(hub.handoff_read()['memory']['available'])
                self.assertEqual(hub.report(state='finished', body='Done')['report']['state'], 'finished')

    def test_structured_sessions_act_through_their_managed_anchor(self):
        from lib.control.session_hub import Hub
        row = self.launch(transport='structured')
        sid = row['session_id']
        with self.hub.database() as db, db.transaction(write=True) as c:
            current = self.hub.get(sid)
            current['native_binding'] = dict(generation=current['generation'], native_id='conv-utility')
            self.hub._save(c, current)
        worker = Hub(self.config, env=dict(self.env, ASHA_MANAGED_SESSION_ID=sid,
                                           CLAUDE_CODE_SESSION_ID='conv-other'), tmux=self.tmux)
        with mock.patch.object(worker, 'structured_actor', return_value=(worker.get(sid), 'key')):
            self.assertEqual(worker.report(state='finished', body='Utility done')['report']['state'], 'finished')
            self.assertTrue(worker.handoff_read()['memory']['available'])
