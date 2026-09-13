import contextlib
import io
import json
from unittest import mock
from tests.python.test_control_session_experience import ExperienceFixture, report
from lib.control import hub_cli


class ExperienceCLI(ExperienceFixture):
    def call(self, *args):
        output = io.StringIO()
        with mock.patch.object(hub_cli, 'Hub', return_value=self.hub), contextlib.redirect_stdout(output):
            code = hub_cli.dispatch(['experience', *args, '--json'], env=self.env)
        return code, json.loads(output.getvalue())

    def test_policy_reads_are_off_without_mutation_and_explicit_edits_cas(self):
        self.assertEqual(self.call('policy', '--project', str(self.project))[1]['mode'], 'capture')
        code, value = self.call('policy', '--project', str(self.project), '--mode', 'off', '--revision', '1')
        self.assertEqual(code, 0); self.assertEqual(value['mode'], 'off')

    def test_report_list_show_stats_and_pending_use_same_public_names(self):
        captured = self.capture()
        self.assertEqual(self.call('list', '--project', str(self.project))[1]['total'], 1)
        self.assertEqual(self.call('show', captured['report_id'])[1]['report_id'], captured['report_id'])
        self.assertEqual(self.call('stats', '--project', str(self.project))[1]['completions']['explicit_reports'], 1)
        self.assertEqual(self.call('pending', '--project', str(self.project))[1]['rows'], [])

    def test_structured_explicit_envelope_preserves_plain_result_compatibility(self):
        from lib.control.session_experience import structured_completion
        self.hub._update(self.sid, result_contract='asha.session-result.v1')
        value = json.dumps({'contract':'asha.session-result.v1', 'result':'Task done', 'experience':report()})
        receipt = structured_completion(self.hub, self.hub.get(self.sid), 'turn-fixture', value)
        self.assertEqual(receipt['summary'], 'Task done')
        self.assertEqual(receipt['capture']['status'], 'captured')
        plain = structured_completion(self.hub, self.row, 'another-turn', 'ordinary free-form prose')
        self.assertEqual(plain, {'summary':'ordinary free-form prose'})

    def test_manual_review_packet_is_available_without_dispatch(self):
        captured = self.capture()
        code, value = self.call('packet', captured['report_id'])
        self.assertEqual(code, 0)
        self.assertIn('packet_digest', value)
        self.assertIn(captured['report_id'], value['packet'])
        self.assertTrue(all(not row['utility_id'] for row in self.experience.show(captured['report_id'])['reviews']))

    def test_structured_key_cannot_hide_changed_optional_content(self):
        first = self.experience.capture(self.row, source='completion', key='structured-key', submitted_body=json.dumps(report()).encode())
        changed = report(); changed['summary'] = 'A different observation'
        second = self.experience.capture(self.row, source='completion', key='structured-key', submitted_body=json.dumps(changed).encode())
        self.assertIsNotNone(first['report_id'])
        self.assertEqual(second['status'], 'invalid')

    def test_report_envelope_bounds_repeated_guidance_history(self):
        from lib.control.session_experience import canonical
        import uuid
        with self.hub.database() as db, db.transaction(write=True) as c:
            for i in range(101):
                manifest = {'selected':['rule'], 'supplied':[{'id':'rule', 'version':'a'*64, 'action':'x'*3000}]}
                c.execute('INSERT INTO hub_guidance_exposures VALUES(?,?,?,?,?,?,?,?)',
                    (str(uuid.uuid4()), self.sid, 1, 'delivery'+str(i), self.pid, 'supplied', canonical(manifest), i))
        saved = self.capture()
        envelope = self.experience.show(saved['report_id'])['envelope']
        self.assertLess(len(canonical(envelope).encode()), 24 * 1024)
        self.assertEqual(envelope['supplied_guidance'], [{'id':'rule', 'version':'a'*64}])
