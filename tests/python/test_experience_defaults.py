"""Amendment C1-C4 defaults, completion, advisory review and Room authority."""
import contextlib
import io
import json
import uuid
from unittest import mock

from tests.python.test_control_session_experience import ExperienceFixture, report
from lib.control import experience_cli
from lib.control.store import StoreError


class DefaultsFixture(ExperienceFixture):
    def config_default(self, value):
        path = self.root / 'user-config.json'
        path.write_text(json.dumps({'session_experience': value}))
        self.hub.env['ASHA_CONFIG'] = str(path)
        return path

    def call(self, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = experience_cli.dispatch(self.hub, [*args, '--json'])
        return code, json.loads(output.getvalue())


class C1PolicyDefaults(DefaultsFixture):
    def test_default_precedence_clear_and_read_without_initialization(self):
        from lib.control.session_experience import Experiences
        from lib.control.session_hub import Hub
        self.config_default({'default_mode': 'review'})
        self.assertEqual(self.experience.policy(self.pid)['source'], 'project')
        cleared = self.experience.clear_policy(str(self.project))
        self.assertEqual((cleared['mode'], cleared['source']), ('review', 'default'))
        self.assertLess(cleared['revision'], 0)
        with mock.patch.object(Hub, 'initialize', side_effect=AssertionError('read migrated')):
            self.assertEqual(Experiences(self.hub).policy(self.pid), cleared)
            with mock.patch.object(self.hub, 'initialized', return_value=False):
                fresh = Experiences(self.hub).policy(self.pid)
                self.assertEqual((fresh['mode'], fresh['source']), ('review', 'default'))
                self.assertLess(fresh['revision'], 0)
        from lib.control.experience_review import backend_support
        self.assertFalse(backend_support()[0])

    def test_clear_does_not_reuse_project_revision_or_accept_stale_cas(self):
        original = self.experience.policy(self.pid)
        cleared = self.experience.clear_policy(str(self.project), expected_revision=original['revision'])
        replacement = self.experience.set_policy(str(self.project), 'review')
        self.assertGreater(replacement['revision'], original['revision'])
        self.assertNotEqual(cleared['revision'], original['revision'])
        with self.assertRaisesRegex(StoreError, 'revision changed'):
            self.experience.set_policy(str(self.project), 'off', expected_revision=original['revision'])
        self.assertEqual(self.experience.policy(self.pid), replacement)

    def test_effective_default_modes_have_distinct_read_only_revision_fingerprints(self):
        self.experience.clear_policy(str(self.project))
        revisions = {}
        with mock.patch.object(self.hub, 'initialize', side_effect=AssertionError('policy read migrated')):
            for mode in ('off', 'capture', 'review'):
                self.config_default({'default_mode': mode})
                revisions[mode] = self.experience.policy(self.pid)['revision']
        self.assertEqual(len(set(revisions.values())), 3)
        self.config_default({'default_mode': 'capture'})
        self.assertEqual(self.experience.policy(self.pid)['revision'], revisions['capture'])
        with self.assertRaisesRegex(StoreError, 'revision changed'):
            self.experience.set_policy(str(self.project), 'off', expected_revision=revisions['review'])

    def test_clear_returns_to_new_default_epoch_and_repeated_clear_is_idempotent(self):
        self.config_default({'default_mode': 'capture'})
        first = self.experience.clear_policy(str(self.project))
        self.assertEqual(self.experience.clear_policy(str(self.project)), first)
        project = self.experience.set_policy(str(self.project), 'capture', expected_revision=first['revision'])
        self.assertGreater(project['revision'], 1)
        second = self.experience.clear_policy(str(self.project))
        self.assertNotEqual(first['revision'], second['revision'])
        with self.assertRaisesRegex(StoreError, 'revision changed'):
            self.experience.set_policy(str(self.project), 'off', expected_revision=first['revision'])

    def test_legacy_policy_revision_highwater_survives_additive_schema_upgrade(self):
        captured = self.capture()
        with self.hub.database() as db, db.transaction(write=True) as c:
            c.execute('DROP TABLE IF EXISTS hub_experience_policy_epochs')
            c.execute('UPDATE hub_experience_policies SET revision=8 WHERE project_id=?', (self.pid,))
            c.execute('UPDATE hub_experience_reviews SET policy_revision=12 WHERE report_id=?', (captured['report_id'],))
        replacement = self.experience.set_policy(str(self.project), 'review', expected_revision=8)
        self.assertEqual(replacement['revision'], 13)
        self.assertLess(self.experience.clear_policy(str(self.project))['revision'], 0)

    def test_invalid_default_is_off_and_doctor_reports_exact_key(self):
        from lib.control import doctor
        self.config_default({'default_mode': True})
        with self.hub.database() as db, db.transaction(write=True) as c:
            c.execute('DELETE FROM hub_experience_policies')
        policy = self.experience.policy(self.pid)
        self.assertEqual((policy['mode'], policy['source']), ('off', 'builtin'))
        probe = doctor.DEFAULT_PROBES['session-experience']
        result = doctor.run_doctor(self.config, probes={'session-experience': probe}, env=self.hub.env)
        self.assertIn('session_experience.default_mode', json.dumps(result))
        self.assertIn('mismatch', json.dumps(result))

    def test_cli_mode_read_and_set_without_revision_and_clear_cas(self):
        code, policy = self.call('policy', '--project', str(self.project), '--mode', 'review')
        self.assertEqual((code, policy['revision']), (0, 2))
        with self.assertRaisesRegex(StoreError, 'revision changed'):
            self.experience.clear_policy(str(self.project), expected_revision=1)
        code, policy = self.call('policy', '--project', str(self.project), '--clear', '--revision', '2')
        self.assertEqual((code, policy['mode'], policy['source']), (0, 'off', 'builtin'))

    def test_public_doctor_fails_invalid_user_default(self):
        import os
        import subprocess
        path = self.config_default({'default_mode': 'enabled'})
        script = ('source lib/doctor.sh\n'
                  'bash() { return 0; }\n'
                  '_asha_doctor_workspace_section() { return 0; }\n'
                  '_asha_doctor_imported_skills_section() { return 0; }\n'
                  '_asha_doctor_session_profile_section() { return 0; }\n'
                  'asha_doctor_main codex')
        output = subprocess.run(['bash', '-c', script], text=True, capture_output=True,
                                env=dict(self.hub.env, PATH=os.environ['PATH'], LANG='C.UTF-8', TERM='dumb'))
        self.assertEqual(output.returncode, 1, output.stdout + output.stderr)
        self.assertIn('session_experience.default_mode', output.stdout)

    def test_invalid_config_shapes_never_enable_and_configured_roots_still_resolve(self):
        from lib.control.orchestration.projects import configured_roots
        for value in [None, [], {}, {'default_mode': 'enabled'}, {'default_mode': 1}]:
            self.config_default(value)
            with mock.patch.object(self.experience, 'available', return_value=False):
                self.assertEqual(self.experience.policy(self.pid)['mode'], 'off')
        path = self.config_default({'default_mode': 'capture'})
        path.write_text(json.dumps({'project_roots': ['/fixture'], 'session_experience': {'default_mode': 'capture'}}))
        self.assertEqual(configured_roots(self.hub.env), ['/fixture'])
        with mock.patch.object(self.experience, 'available', return_value=False):
            self.assertEqual(self.experience.policy(self.pid)['mode'], 'capture')


class C2CompletionCapture(DefaultsFixture):
    def test_worker_brief_contract_only_when_enabled(self):
        from lib.control import session_hub
        self.hub.close(self.sid, force=True)
        with mock.patch.object(session_hub, 'open_room', wraps=session_hub.open_room) as opened:
            opened_row = self.launch()
        brief = opened.call_args.kwargs['prompt']
        self.assertIn('--experience-file', brief)
        self.assertIn('asha.session-experience.v1', brief)
        self.assertLess(len(brief.encode()), 2400)
        self.experience.set_policy(str(self.project), 'off')
        self.hub.close(opened_row['session_id'], force=True)
        with mock.patch.object(session_hub, 'open_room', wraps=session_hub.open_room) as opened:
            self.launch()
        self.assertNotIn('--experience-file', opened.call_args.kwargs['prompt'])

    def test_finished_request_key_attaches_once_and_preserves_result(self):
        with self.acting_as(self.sid):
            first = self.hub.report(state='finished', body='Exact recorded result')
            request = first['experience_request']
            self.assertIn('asha.session-experience.v1', request['text'])
            self.assertIn('16 KiB', request['text'])
            self.assertIn('--key ' + request['key'], request['text'])
            self.assertLess(len(request['text'].encode()), 1600)
            self.assertEqual(self.hub.report(state='finished')['experience_request']['key'], request['key'])
            follow = self.hub.report(state='finished', body='Replacement attempt', experience_file=self.file(), key=request['key'])
            again = self.hub.report(state='finished', experience_file=self.file(), key=request['key'])
        self.assertEqual(follow['result'], 'Exact recorded result')
        self.assertEqual(follow['capture'], again['capture'])
        self.assertEqual(self.experience.page(self.pid)['total'], 1)
        self.assertEqual(again['experience_request']['status'], 'answered')

    def test_exit_retains_missing_reason_and_does_not_manufacture_report(self):
        with self.acting_as(self.sid):
            first = self.hub.report(state='finished', body='Done')
            ended = self.hub.observe('session-ended')
        self.assertEqual(ended['capture']['status'], 'missing')
        self.assertEqual(ended['capture']['reason'], 'exited-before-capture')
        with self.hub.database() as db, db.transaction() as c:
            receipt = c.execute('SELECT * FROM hub_experience_captures WHERE delivery_key=?',
                                (first['experience_request']['key'],)).fetchone()
        self.assertEqual(receipt['reason'], 'exited-before-capture')
        self.assertEqual(self.experience.page(self.pid)['total'], 0)

    def test_supervisor_records_missing_when_terminal_exits_without_hook(self):
        from lib.control.experience_review import reconcile
        with self.acting_as(self.sid):
            self.hub.report(state='finished', body='Done')
        self.tmux.dead = True
        reconcile(self.hub)
        self.assertEqual(self.hub.get(self.sid)['capture'].get('reason'), 'exited-before-capture')
        self.assertEqual(self.experience.page(self.pid)['total'], 0)

    def test_off_silence_and_structured_do_not_request_experience(self):
        with self.acting_as(self.sid):
            self.experience.set_policy(str(self.project), 'off')
            self.assertNotIn('experience_request', self.hub.report(state='finished', body='Done'))
            self.experience.set_policy(str(self.project), 'capture')
            marker = self.project / 'Work/markers/silence'
            marker.parent.mkdir(parents=True, exist_ok=True); marker.touch()
            with self.assertRaisesRegex(StoreError, 'silence'):
                self.hub.report(state='finished', body='Done')
            marker.unlink()
            self.hub._update(self.sid, transport='structured')
            self.assertFalse(self.experience.completion_enabled(self.hub.get(self.sid)))


class ReviewFixture(DefaultsFixture):
    def publication(self):
        from lib.control.session_closure import memory_v2
        self.hub.env['ASHA_SESSION_ID'] = 'chair-native-save'
        snapshot = memory_v2.read_published_snapshot(self.project)
        return memory_v2.publish(self.project, snapshot.active_context.decode(), snapshot.decisions.decode(),
                                 expected_preimages=memory_v2.snapshot_digests(snapshot))

    def review_bytes(self, rid):
        from lib.control.experience_review import packet, packet_digest
        saved = self.experience.show(rid)
        body = packet(self.hub, saved['reviews'][-1]['review_id'])
        return json.dumps({'contract': 'asha.experience-review.v1', 'report_digest': saved['digest'],
            'packet_digest': packet_digest(body), 'findings': [{'key': 'retry', 'verdict': 'supported',
            'evidence_ids': ['check'], 'contradictory_evidence_ids': [], 'inference': 'May prevent lost edits',
            'uncertainty': 'One fixture', 'scope': 'This project', 'destination': 'reusable-candidate',
            'check': 'Compare concurrent writes', 'benefit': 'Preserve changes', 'regressions': 'False conflicts'}]}).encode()


class C3SaveAdvisoryReview(ReviewFixture):
    def test_unreviewed_selected_capture_reports_are_oldest_first_paginated_and_revision_scoped(self):
        first = self.capture(); second = self.capture()
        code, page = self.call('unreviewed', '--project', str(self.project), '--limit', '1')
        self.assertEqual(code, 0)
        self.assertEqual(page['total'], 2)
        self.assertEqual(page['rows'][0]['report_id'], first['report_id'])
        self.assertEqual(page['rows'][0]['selection_reason'], 'observation-trigger')
        self.assertEqual(page['next_offset'], 1)
        experience_cli.manual_review(self.hub, self.pid, first['report_id'], self.review_bytes(first['report_id']))
        self.assertEqual(self.call('unreviewed', '--project', str(self.project))[1]['total'], 1)
        self.experience.set_policy(str(self.project), 'review')
        refreshed = self.call('unreviewed', '--project', str(self.project))[1]
        self.assertEqual(refreshed['total'], 2)
        self.assertEqual(refreshed['policy_revision'], 2)

    def test_chair_native_save_identity_retains_existing_copilot_resolver_fallback(self):
        import save_identity
        self.hub.env['ASHA_HARNESS'] = 'copilot'
        with mock.patch.object(save_identity, 'resolve', return_value='copilot-native') as resolve:
            saver = self.experience.saving_actor(self.pid, required=True, project=self.project)
        self.assertEqual(saver['session_id'], 'copilot-native')
        resolve.assert_called_once_with(self.project, 'copilot')

    def test_unreviewed_marks_own_lineage_and_current_revision_packet_is_read_only(self):
        self.publication()
        self.hub._update(self.sid, native_id='chair-native-save')
        rid = self.capture()['report_id']
        page = self.call('unreviewed', '--project', str(self.project))[1]
        self.assertEqual(page['rows'][0]['skip_reason'], 'own-session-lineage')
        self.experience.set_policy(str(self.project), 'review')
        before = len(self.experience.show(rid)['reviews'])
        packet = self.call('packet', rid)[1]
        self.assertIsNone(packet['review_id'])
        self.assertEqual(len(self.experience.show(rid)['reviews']), before)

    def test_save_review_five_report_cap_retry_and_next_publication(self):
        publication = self.publication()
        reports = [self.capture()['report_id'] for _ in range(6)]
        for rid in reports[:5]:
            receipt = experience_cli.manual_review(self.hub, self.pid, rid, self.review_bytes(rid), publication=publication)
            self.assertEqual(receipt['reviewer'], 'advisory-save-review')
        self.assertEqual(experience_cli.manual_review(self.hub, self.pid, reports[0], self.review_bytes(reports[0]),
                                                     publication=publication)['status'], 'completed')
        with self.assertRaisesRegex(StoreError, 'five'):
            experience_cli.manual_review(self.hub, self.pid, reports[5], self.review_bytes(reports[5]), publication=publication)
        experience_cli.manual_review(self.hub, self.pid, reports[5], self.review_bytes(reports[5]), publication=self.publication())
        self.assertEqual(self.experience.stats(self.pid)['reviewers']['advisory-save-review'], 6)

    def test_own_native_lineage_is_skipped_durably_before_result_decode(self):
        publication = self.publication()
        self.hub._update(self.sid, native_id='chair-native-save')
        rid = self.capture()['report_id']
        result = experience_cli.manual_review(self.hub, self.pid, rid, b'{}', publication=publication)
        self.assertEqual((result['status'], result['reason']), ('skipped', 'own-session-lineage'))
        with self.hub.database() as db, db.transaction() as c:
            skip = c.execute('SELECT reason FROM hub_experience_save_reviews WHERE report_id=?', (rid,)).fetchone()
        self.assertEqual(skip[0], 'own-session-lineage')
        self.assertNotEqual(self.experience.show(rid)['reviews'][0]['status'], 'completed')

    def test_completed_review_does_not_survive_clear_and_reenable_as_current_revision(self):
        rid = self.capture()['report_id']
        experience_cli.manual_review(self.hub, self.pid, rid, self.review_bytes(rid))
        self.assertEqual(self.call('unreviewed', '--project', str(self.project))[1]['total'], 0)
        self.experience.clear_policy(str(self.project))
        self.experience.set_policy(str(self.project), 'review')
        self.assertEqual(self.call('unreviewed', '--project', str(self.project))[1]['total'], 1)

    def test_capture_freezes_native_lineage_when_source_session_identity_later_changes(self):
        self.hub._update(self.sid, native_id='original-native')
        rid = self.capture()['report_id']
        self.hub._update(self.sid, native_id='later-native')
        publication = self.publication()
        self.hub.env['ASHA_SESSION_ID'] = 'original-native'
        receipt = experience_cli.manual_review(self.hub, self.pid, rid, b'{}', publication=publication)
        self.assertEqual((receipt['status'], receipt['reason']), ('skipped', 'own-session-lineage'))

    def test_native_custody_takes_precedence_and_policy_off_suppresses_listing(self):
        rid = self.capture()['report_id']
        with self.hub.database() as db, db.transaction(write=True) as c:
            c.execute('UPDATE hub_experience_reviews SET utility_id=? WHERE report_id=?', (str(uuid.uuid4()), rid))
        self.assertEqual(self.call('unreviewed', '--project', str(self.project))[1]['total'], 0)
        with self.assertRaises(StoreError):
            experience_cli.manual_review(self.hub, self.pid, rid, self.review_bytes(rid), publication=self.publication())
        self.experience.set_policy(str(self.project), 'off')
        self.assertEqual(self.call('unreviewed', '--project', str(self.project))[1]['rows'], [])

    def test_save_review_label_survives_pending_and_adopted_provenance(self):
        from lib.control import experience_adoption as adoption
        import learnings_manager as lm
        rid = self.capture()['report_id']; publication = self.publication()
        experience_cli.manual_review(self.hub, self.pid, rid, self.review_bytes(rid), publication=publication)
        finding = adoption.pending(self.hub, self.pid)['rows'][0]
        self.assertEqual(finding['reviewer'], 'advisory-save-review')
        decision = {'review_id': finding['review_id'], 'observation_key': 'retry',
            'finding_digest': finding['finding_digest'], 'save_key': str(uuid.uuid4()), 'disposition': 'propose',
            'reason': 'Reviewed', 'rule_id': 'save-review', 'trigger': 'When saving', 'action': 'Compare digests'}
        with mock.patch.object(lm, 'learnings_dir', return_value=self.root / 'learning-fixture'):
            adoption.dispose(self.hub, str(self.project), decision, publication, save_session_id='chair-native-save')
            source = lm.load('save-review').evidence[0].source_provenance
        self.assertEqual(source['reviewer'], 'advisory-save-review')


class C4RoomAuthority(ReviewFixture):
    def room_actor(self):
        source = self.capture()['report_id']
        self.hub.close(self.sid, force=True)
        room = self.launch(profile='room')
        self.hub.env.update(ASHA_HUB_SESSION_ID=room['session_id'], ASHA_HUB_GENERATION=str(room['generation']),
                            ASHA_SESSION_PROFILE='room')
        self.enterContext(mock.patch('lib.control.harness.caller_descends_from', return_value=True))
        return source, room

    def decision(self, rid):
        from lib.control.experience_adoption import pending
        finding = next(item for item in pending(self.hub, self.pid)['rows'] if item['report_id'] == rid)
        return {'review_id': finding['review_id'], 'observation_key': 'retry', 'finding_digest': finding['finding_digest'],
                'save_key': str(uuid.uuid4()), 'disposition': 'reject', 'reason': 'Insufficient generality'}

    def verifier(self, expected):
        import types
        def verify(hub, actor, receipt):
            if receipt != expected or receipt.get('hub_session_id') != actor['session_id'] or receipt.get('hub_generation') != actor['generation']:
                raise StoreError('controller-retained explicit-save publication required')
            return receipt
        module = types.ModuleType('lib.control.session_publication')
        module.verify_publication = mock.Mock(side_effect=verify)
        self.enterContext(mock.patch.dict('sys.modules', {'lib.control.session_publication': module}))
        return module.verify_publication

    def test_verified_room_reviews_and_disposes_with_controller_retained_publication(self):
        from lib.control import experience_adoption as adoption
        rid, room = self.room_actor()
        reviewed = experience_cli.manual_review(self.hub, self.pid, rid, self.review_bytes(rid))
        self.assertEqual(reviewed['status'], 'completed')
        publication = dict(self.publication(), hub_session_id=room['session_id'], hub_generation=room['generation'])
        verifier = self.verifier(publication)
        decision = self.decision(rid)
        result = adoption.dispose(self.hub, str(self.project), decision, publication, save_session_id=room['session_id'])
        self.assertEqual(result['state'], 'completed')
        verifier.assert_called_once()
        with self.assertRaisesRegex(StoreError, 'publication'):
            adoption.dispose(self.hub, str(self.project), decision, dict(publication, publication_id=str(uuid.uuid4())),
                             save_session_id=room['session_id'])

    def test_room_refuses_own_lineage_review_and_disposition_even_across_generation(self):
        from lib.control import experience_adoption as adoption
        rid = self.capture()['report_id']
        experience_cli.manual_review(self.hub, self.pid, rid, self.review_bytes(rid))
        decision = self.decision(rid)
        self.hub._update(self.sid, profile='room', generation=2)
        self.hub.env.update(ASHA_HUB_SESSION_ID=self.sid, ASHA_HUB_GENERATION='2', ASHA_SESSION_PROFILE='room')
        self.enterContext(mock.patch('lib.control.harness.caller_descends_from', return_value=True))
        with self.assertRaisesRegex(StoreError, 'lineage'):
            experience_cli.manual_review(self.hub, self.pid, rid, self.review_bytes(rid))
        publication = dict(self.publication(), hub_session_id=self.sid, hub_generation=2)
        self.verifier(publication)
        with self.assertRaisesRegex(StoreError, 'lineage'):
            adoption.dispose(self.hub, str(self.project), decision, publication, save_session_id=self.sid)

    def test_room_project_scope_and_policy_operator_restriction_remain(self):
        from lib.control import experience_adoption as adoption
        from lib.control.session_closure import memory_v2
        rid, room = self.room_actor()
        with self.assertRaisesRegex(StoreError, 'project'):
            experience_cli.manual_review(self.hub, 'other-project', rid, self.review_bytes(rid))
        other = self.root / 'other-project'; other.mkdir(); memory_v2.initialize(other)
        with self.assertRaisesRegex(StoreError, 'project'):
            adoption.dispose(self.hub, str(other), {}, {}, save_session_id=room['session_id'])
        with self.assertRaises(StoreError):
            self.experience.set_policy(str(self.project), 'off')

    def test_worker_managed_coordinator_and_worker_ancestry_stay_refused(self):
        rid = self.capture()['report_id']
        raw = self.review_bytes(rid)
        for extra in [{'ASHA_HUB_SESSION_ID': self.sid, 'ASHA_HUB_GENERATION': '1', 'ASHA_SESSION_PROFILE': 'worker'},
                      {'ASHA_MANAGED_SESSION_ID': 'managed'}, {'ASHA_CONTROL_MANAGED': '1'},
                      {'ASHA_ORCHESTRATION_COORDINATOR_ID': 'coordinator'}]:
            with self.subTest(extra=extra), mock.patch.dict(self.hub.env, extra), self.assertRaises(StoreError):
                experience_cli.manual_review(self.hub, self.pid, rid, raw)
        with mock.patch('lib.control.harness.caller_descends_from', return_value=True), self.assertRaisesRegex(StoreError, 'ancestry'):
            experience_cli.manual_review(self.hub, self.pid, rid, raw)

    def test_worker_profile_cannot_inherit_room_saving_authority(self):
        rid, room = self.room_actor()
        self.hub.env['ASHA_SESSION_PROFILE'] = 'worker'
        with self.assertRaisesRegex(StoreError, 'worker'):
            experience_cli.manual_review(self.hub, self.pid, rid, self.review_bytes(rid))

    def test_room_requires_verified_actor_and_declared_profile_is_insufficient(self):
        rid, room = self.room_actor()
        with mock.patch.object(self.hub, 'actor', side_effect=StoreError('actor proof failed')), self.assertRaisesRegex(StoreError, 'actor proof'):
            experience_cli.manual_review(self.hub, self.pid, rid, self.review_bytes(rid))
        self.hub._update(room['session_id'], profile='worker')
        with self.assertRaisesRegex(StoreError, 'Room'):
            experience_cli.manual_review(self.hub, self.pid, rid, self.review_bytes(rid))


class C3LegacyAdoptionReplay(ReviewFixture):
    def legacy_intent(self, *, interrupted=False):
        from lib.control import experience_adoption as adoption
        rid = self.capture()['report_id']
        experience_cli.manual_review(self.hub, self.pid, rid, self.review_bytes(rid))
        publication = self.publication()
        finding = adoption.pending(self.hub, self.pid)['rows'][0]
        decision = {'review_id': finding['review_id'], 'observation_key': 'retry',
                    'finding_digest': finding['finding_digest'], 'save_key': str(uuid.uuid4()),
                    'disposition': 'reject', 'reason': 'Retained pre-amendment decision'}
        expected = None
        if interrupted:
            with mock.patch.object(adoption, '_complete', side_effect=OSError('interrupted intent receipt')):
                with self.assertRaisesRegex(OSError, 'interrupted'):
                    adoption.dispose(self.hub, str(self.project), decision, publication, save_session_id='chair-native-save')
        else:
            expected = adoption.dispose(self.hub, str(self.project), decision, publication, save_session_id='chair-native-save')
        with self.hub.database() as db, db.transaction(write=True) as c:
            row = c.execute('SELECT disposition_id,payload FROM hub_experience_dispositions WHERE save_key=?',
                            (decision['save_key'],)).fetchone()
            prior = json.loads(row['payload'])
            del prior['source']['reviewer']
            c.execute('UPDATE hub_experience_dispositions SET payload=? WHERE disposition_id=?',
                      (json.dumps(prior), row['disposition_id']))
        return decision, publication, expected, row['disposition_id']

    def test_completed_legacy_receipt_replays_without_rewriting_missing_reviewer_provenance(self):
        from lib.control import experience_adoption as adoption
        decision, publication, expected, did = self.legacy_intent()
        replay = adoption.dispose(self.hub, str(self.project), decision, publication, save_session_id='chair-native-save')
        self.assertEqual(replay, expected)
        with self.hub.database() as db, db.transaction() as c:
            saved = json.loads(c.execute('SELECT payload FROM hub_experience_dispositions WHERE disposition_id=?', (did,)).fetchone()[0])
        self.assertNotIn('reviewer', saved['source'])

    def test_interrupted_legacy_intent_completes_with_original_frozen_source(self):
        from lib.control import experience_adoption as adoption
        decision, publication, _, did = self.legacy_intent(interrupted=True)
        result = adoption.dispose(self.hub, str(self.project), decision, publication, save_session_id='chair-native-save')
        self.assertEqual(result['state'], 'completed')
        with self.hub.database() as db, db.transaction() as c:
            saved = json.loads(c.execute('SELECT payload FROM hub_experience_dispositions WHERE disposition_id=?', (did,)).fetchone()[0])
        self.assertNotIn('reviewer', saved['source'])

    def test_legacy_compatibility_does_not_accept_other_intent_or_present_reviewer_changes(self):
        from lib.control import experience_adoption as adoption
        decision, publication, _, did = self.legacy_intent()
        with self.assertRaisesRegex(StoreError, 'different intent'):
            adoption.dispose(self.hub, str(self.project), dict(decision, reason='Changed reason'), publication,
                             save_session_id='chair-native-save')
        with self.hub.database() as db, db.transaction(write=True) as c:
            saved = json.loads(c.execute('SELECT payload FROM hub_experience_dispositions WHERE disposition_id=?', (did,)).fetchone()[0])
            saved['source']['reviewer'] = 'advisory-save-review'
            c.execute('UPDATE hub_experience_dispositions SET payload=? WHERE disposition_id=?', (json.dumps(saved), did))
        with self.assertRaisesRegex(StoreError, 'different intent'):
            adoption.dispose(self.hub, str(self.project), decision, publication, save_session_id='chair-native-save')
