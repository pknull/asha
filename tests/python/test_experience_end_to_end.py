"""Frozen report -> one review -> explicit adoption -> later native assignment."""
import json
import uuid
from pathlib import Path
from unittest import mock
from tests.python.test_control_session_experience import ExperienceFixture, report
from lib.control import experience_review as review, sessions
from lib.control.session_store import SessionStore
from lib.control.session_closure import memory_v2
import learnings_manager as lm


class ExperienceJourney(ExperienceFixture):
    def launch(self, **changes):
        changes.setdefault('harness', 'codex')
        return super().launch(**changes)

    def reviewed(self):
        self.experience.set_policy(str(self.project), 'review', expected_revision=1)
        captured = self.capture()
        row = self.experience.show(captured['report_id'])['reviews'][0]
        with mock.patch.object(review, 'backend_support', return_value=(True, 'fixture-only')):
            reserved = review.reserve(self.hub, row['review_id'])
            with SessionStore(self.config) as store:
                state = store.claim_owner(reserved['utility_id'])
                message = store.claim_turn(state['session_id'], state['generation'])
                result = {'contract': 'asha.experience-review.v1', 'report_digest': captured['digest'],
                    'packet_digest': reserved['packet_digest'], 'findings': [{'key': 'retry', 'verdict': 'supported',
                    'evidence_ids': ['check'], 'contradictory_evidence_ids': [], 'inference': 'Comparison may avoid loss.',
                    'uncertainty': 'One fixture', 'scope': 'Shared save', 'destination': 'reusable-candidate',
                    'check': 'Two publishers', 'benefit': 'Preserve contributions', 'regressions': 'False refusals'}]}
                class Transport:
                    input_not_submitted = False
                    def __init__(self, argv, **kw):
                        self.argv = argv
                    def events(self, prompt, *, cancelled):
                        assert not cancelled()
                        yield 'completed', {'summary': json.dumps(result)}
                sessions.run_turn(store, store.get(state['session_id']), message, env=self.env,
                    root=Path(__file__).resolve().parents[2], transport_factory=Transport)
                # The actual utility owner exits after its one turn. This
                # in-process transport fixture retires that owner's PID explicitly.
                with store.db.transaction(write=True) as c:
                    c.execute('UPDATE managed_sessions SET owner_pid=NULL,owner_identity=NULL WHERE session_id=?', (state['session_id'],))
        return captured, row

    def test_interrupted_disposition_reconciles_once_and_guidance_reaches_assignment(self):
        from lib.control import experience_adoption as adoption
        bundle = self.root / 'learning-fixture'
        self.enterContext(mock.patch.object(lm, 'learnings_dir', return_value=bundle))
        lm.save(lm.Learning('initial-guidance', 'Before drafting', 'Retain the original comparison baseline', state='active'), project_dir=self.project)
        initial = self.hub.send(self.sid, 'Run the publication fixture', key=str(uuid.uuid4()), learning_ids=['initial-guidance'])
        emitted = next(m for m in self.hub.messages(self.sid) if m['message_id'] == initial['message_id'])
        self.assertIn('Retain the original comparison baseline', emitted['body'])
        with self.acting_as(self.sid):
            self.hub.acknowledge(initial['message_id'], delivery_digest=emitted['delivery_digest'])
        captured, row = self.reviewed()
        self.assertEqual(self.experience.show(captured['report_id'])['envelope']['harness'], 'codex')
        findings = adoption.pending(self.hub, self.pid)
        self.assertEqual(len(findings['rows']), 1)
        finding = findings['rows'][0]
        snapshot = memory_v2.read_published_snapshot(self.project)
        publication = memory_v2.publish(self.project, snapshot.active_context.decode(), snapshot.decisions.decode(),
                                        expected_preimages=memory_v2.snapshot_digests(snapshot))
        decision = {'review_id': row['review_id'], 'observation_key': 'retry', 'finding_digest': finding['finding_digest'],
                    'save_key': str(uuid.uuid4()), 'disposition': 'propose', 'reason': 'Evidence inspected',
                    'rule_id': 'compare-save', 'trigger': 'When publishing', 'action': 'Compare the baseline'}
        with mock.patch.object(adoption, '_complete', side_effect=OSError('receipt write interrupted')):
            with self.assertRaises(OSError):
                adoption.dispose(self.hub, str(self.project), decision, publication, save_session_id='chair')
        receipt = adoption.dispose(self.hub, str(self.project), decision, publication, save_session_id='chair')
        again = adoption.dispose(self.hub, str(self.project), decision, publication, save_session_id='chair')
        self.assertEqual(receipt, again)
        self.assertEqual(len(lm.load('compare-save').evidence), 1)
        self.assertEqual(lm.load('compare-save').evidence[0].session_id, self.sid)
        second_project = self.root / 'second-project'; second_project.mkdir(); memory_v2.initialize(second_project)
        lm.corroborate('compare-save', project_dir=second_project, session_id='independent-session-two', reason='Another observation')
        lm.corroborate('compare-save', project_dir=self.project, session_id='independent-session-three', reason='A later observation')
        self.assertTrue(lm.activate_if_eligible('compare-save', project_dir=self.project))
        self.hub.close(self.sid, force=True)
        from lib.control import session_hub
        with mock.patch.object(session_hub, 'open_room', wraps=session_hub.open_room) as native:
            later = self.hub.launch(project=str(self.project), prompt='Apply the safe publication change', harness='codex',
                                    learning_ids=['compare-save'])
        self.assertIn('Compare the baseline', native.call_args.kwargs['prompt'])
        manifest = later['guidance'][-1]
        self.assertEqual(manifest['status'], 'supplied')
        self.assertEqual(manifest['manifest']['supplied'][0]['version'], lm.rule_version(lm.load('compare-save')))
        stats = self.experience.stats(self.pid)
        self.assertEqual(stats['guidance']['supplied'], 2)
        self.assertEqual(stats['guidance']['reported_use']['unknown'], 2)
        self.assertIsNone(stats['cost']['tokens'])
        self.assertEqual(stats['reviews']['completed'], 1)
        outcome = report()
        outcome['guidance_feedback'] = [{'id':'compare-save', 'version':lm.rule_version(lm.load('compare-save')),
            'use':'applied', 'evidence_ids':['check'], 'target_failure':'not-observed'}]
        with self.acting_as(later['session_id']):
            self.hub.handoff(None, outcome='no-durable-update', detail='Fixture verification complete')
            later_result = self.hub.report(state='finished', body='Fixture outcome reported',
                experience_file=self.file(outcome), key=str(uuid.uuid4()))
        stats = self.experience.stats(self.pid)
        self.assertEqual(stats['guidance']['reported_use'], {'applied':1, 'not-applied':0, 'not-applicable':0, 'unknown':1})
        self.assertEqual(stats['recurrence']['not-observed'], 1)
        self.assertEqual(stats['recurrence']['unknown'], 1)
        self.assertIsNone(stats['recurrence']['verified_improvement'])
        print('E2E', json.dumps({'source_harness':'codex', 'review_harness':'claude',
            'initial_capture':captured['status'], 'disposition_replay_identical':receipt == again,
            'original_evidence_count':1, 'later_capture':later_result['capture']['status'],
            'guidance':stats['guidance'], 'recurrence':stats['recurrence']}))

    def test_incompatible_or_stale_guidance_is_excluded(self):
        from lib.control.session_guidance import resolve
        bundle = self.root / 'learning-fixture'
        self.enterContext(mock.patch.object(lm, 'learnings_dir', return_value=bundle))
        learning = lm.Learning('codex-only', 'On Codex', 'Check the native seam', state='active', applicability={'harnesses': ['codex']})
        lm.save(learning, project_dir=self.project)
        block, manifest = resolve(self.hub, dict(self.row, harness='claude'), ['codex-only', 'missing'])
        self.assertEqual(block, '')
        self.assertEqual(len(manifest['excluded']), 2)
        block, manifest = resolve(self.hub, dict(self.row, harness='codex'), ['codex-only@'+'0'*64])
        self.assertEqual(block, '')
        self.assertEqual(manifest['excluded'][0]['reason'], 'stale-version')
