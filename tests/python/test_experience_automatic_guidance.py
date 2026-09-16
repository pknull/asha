"""C5 automatic selection at real assignment and acknowledgement seams."""
import json
import unittest
from unittest import mock

from tests.python.test_control_session_closure import ClosureFixture
from lib.control import session_guidance as guidance
from lib.control import hub_cli
import learnings_manager as lm


class AutomaticGuidance(ClosureFixture):
    def setUp(self):
        super().setUp()
        self.enterContext(mock.patch.object(lm, 'learnings_dir', return_value=self.root / 'rules'))

    def rule(self, name, *, scope=None, sessions=0, state='active'):
        evidence = [lm.Evidence('2026-09-16', 'source-' + str(i), 'project', 'Fixture', 'corroborate')
                    for i in range(sessions)]
        lm.save(lm.Learning(name, 'Before a change', 'Check ' + name, state=state,
                           applicability=scope or {}, evidence=evidence), project_dir=self.project)

    def test_automatic_order_scope_evidence_then_id_and_three_rule_bound(self):
        self.rule('global', sessions=9)
        self.rule('harness', scope={'harnesses': ['codex']}, sessions=4)
        self.rule('project-b', scope={'project_ids': ['novel-project']}, sessions=1)
        self.rule('project-a', scope={'project_ids': ['novel-project']}, sessions=1)
        self.rule('wrong-project', scope={'project_ids': ['elsewhere']}, sessions=20)
        self.rule('wrong-harness', scope={'harnesses': ['claude']}, sessions=20)
        row = self.launch(harness='codex')
        manifest = row['guidance'][0]['manifest']
        self.assertEqual(manifest['selection'], 'automatic')
        self.assertEqual([r['id'] for r in manifest['supplied']], ['project-a', 'project-b', 'harness'])
        self.assertLessEqual(len(manifest['planned_block'].encode()), 3072)

    def test_evidence_counts_distinct_source_sessions_and_ignores_repeated_saves(self):
        self.rule('a', sessions=1)
        self.rule('b', sessions=2)
        learning = lm.load('a')
        learning.evidence *= 10
        lm.save(learning, project_dir=self.project)
        row = self.launch()
        self.assertEqual([r['id'] for r in row['guidance'][0]['manifest']['supplied']], ['b', 'a'])

    def test_explicit_none_and_rooms_do_not_automatically_select(self):
        self.rule('automatic')
        self.rule('chosen')
        row = self.launch(learning_ids=['chosen'])
        self.assertEqual(row['guidance'][0]['manifest']['selection'], 'explicit')
        self.assertEqual([r['id'] for r in row['guidance'][0]['manifest']['supplied']], ['chosen'])
        self.hub.stop(row['session_id'])
        for kwargs in ({'learning_ids': []}, {'profile': 'room'}):
            with self.subTest(kwargs=kwargs):
                row = self.launch(**kwargs)
                self.assertEqual(row['guidance'][0]['manifest']['selection'], 'none')
                self.assertEqual(row['guidance'][0]['manifest']['supplied'], [])
                self.hub.stop(row['session_id'])

    def test_send_and_resume_auto_select_but_supply_requires_delivery_receipt(self):
        row = self.launch(learning_ids=[])
        self.rule('later')
        sid = row['session_id']
        message = self.hub.send(sid, 'Follow up', key='auto-message')
        item = self.hub.show(sid)['guidance'][-1]
        self.assertEqual(item['manifest']['selection'], 'automatic')
        self.assertEqual(item['status'], 'queued')
        emitted = self.hub.messages(sid)[0]
        self.assertIn('Check later', emitted['body'])
        with self.acting_as(sid):
            self.hub.acknowledge(message['message_id'], delivery_digest=emitted['delivery_digest'])
        self.assertEqual(self.hub.show(sid)['guidance'][-1]['manifest']['selection'], 'automatic')
        self.hub.stop(sid)
        resumed = self.hub.resume(sid, prompt='Continue')
        self.assertEqual(resumed['guidance'][-1]['manifest']['selection'], 'automatic')
        self.assertEqual(resumed['guidance'][-1]['manifest']['supplied'][0]['id'], 'later')

    def test_automatic_retry_preserves_frozen_selection_when_learnings_change(self):
        row = self.launch(learning_ids=[])
        self.rule('first')
        first = self.hub.send(row['session_id'], 'Follow up', key='repeat')
        self.rule('new', sessions=20)
        retry = self.hub.send(row['session_id'], 'Follow up', key='repeat')
        self.assertEqual(first['message_id'], retry['message_id'])
        emitted = self.hub.messages(row['session_id'])[0]
        self.assertNotIn('Check new', emitted['body'])

    def test_cli_no_learning_is_mutually_exclusive_and_delivers_empty_selection(self):
        with mock.patch.object(hub_cli.Hub, 'launch', return_value={}) as launch:
            self.assertEqual(hub_cli.dispatch(['launch', '--project', str(self.project), '--prompt', 'Work',
                                              '--no-learning', '--json'], env=self.env), 0)
            self.assertEqual(launch.call_args.kwargs['learning_ids'], [])
            with self.assertRaises(SystemExit):
                hub_cli.dispatch(['launch', '--project', str(self.project), '--prompt', 'Work',
                                  '--no-learning', '--learning', 'one'], env=self.env)

    def test_each_terminal_harness_receives_only_its_compatible_automatic_guidance(self):
        for harness in ('claude', 'codex', 'copilot', 'opencode'):
            self.rule(harness + '-rule', scope={'harnesses': [harness]})
        for harness in ('claude', 'codex', 'copilot', 'opencode'):
            with self.subTest(harness=harness):
                row = self.launch(harness=harness)
                supplied = row['guidance'][0]['manifest']['supplied']
                self.assertEqual([rule['id'] for rule in supplied], [harness + '-rule'])
                self.assertEqual(row['guidance'][0]['status'], 'supplied')
                self.hub.stop(row['session_id'])


if __name__ == '__main__':
    unittest.main()
