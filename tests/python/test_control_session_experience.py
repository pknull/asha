"""Experience contracts, immutable custody and close independence."""
import copy
import hashlib
import json
import os
import unittest
import uuid
from unittest import mock

from tests.python.test_control_session_closure import ClosureFixture
from lib.control.store import StoreError


def report(assessment='observations'):
    return {'contract': 'asha.session-experience.v1', 'assessment': assessment,
            'outcome': 'partial', 'summary': 'A failing check was repaired; broader verification remains.',
            'observations': ([{'key': 'retry', 'kind': 'failure-recovery',
                'observed': 'The check failed before the change and passed after.',
                'explanation': 'The omitted comparison may explain the failure.',
                'evidence_ids': ['check'], 'lesson': {'trigger': 'When publishing', 'action': 'Compare the baseline'},
                'applicability': {'harnesses': ['codex'], 'task_kind': 'coding', 'limitations': 'One fixture'},
                'uncertainty': 'No native run yet.'}] if assessment == 'observations' else []),
            'evidence': [{'id': 'check', 'kind': 'agent-attestation', 'text': 'One failing fixture, then passing.'}],
            'guidance_feedback': []}


class ExperienceFixture(ClosureFixture):
    def setUp(self):
        super().setUp()
        from lib.control.session_experience import Experiences
        self.experience = Experiences(self.hub)
        self.row = self.launch()
        self.sid = self.row['session_id']
        self.pid = self.row['project_id']
        self.experience.set_policy(str(self.project), 'capture', expected_revision=0)

    def file(self, value=None):
        path = self.project / 'report.json'
        path.write_text(json.dumps(report() if value is None else value))
        return str(path)

    def capture(self, value=None, **kw):
        with self.acting_as(self.sid):
            return self.hub.report(state='finished', body='Done', experience_file=self.file(value),
                                   key=kw.pop('key', str(uuid.uuid4())), **kw)['capture']


class ExperienceContracts(ExperienceFixture):
    def test_partial_capture_storage_failure_rolls_back_without_blocking_close(self):
        from lib.control.database import Transaction, DatabaseError
        closing = self.hub.close(self.sid)['closure']
        original = Transaction.execute
        def reject_review(c, sql, parameters=()):
            if sql.startswith('INSERT INTO hub_experience_reviews'):
                raise DatabaseError('fixture optional review storage unavailable')
            return original(c, sql, parameters)
        with self.acting_as(self.sid), mock.patch.object(Transaction, 'execute', reject_review):
            result = self.hub.handoff(closing['request_id'], outcome='no-durable-update', detail='Unchanged',
                                      experience_file=self.file())
        self.assertEqual(result['closure_state'], 'acknowledged')
        self.assertEqual(result['capture']['status'], 'invalid')
        self.assertEqual(self.experience.page(self.pid)['total'], 0)

    def test_unavailable_capture_receipt_table_preserves_ordinary_close_ack(self):
        from lib.control.database import Transaction, DatabaseError
        closing = self.hub.close(self.sid)['closure']
        original = Transaction.execute
        def reject_receipt(c, sql, parameters=()):
            if sql.startswith('INSERT OR REPLACE INTO hub_experience_captures'):
                raise DatabaseError('fixture optional capture storage unavailable')
            return original(c, sql, parameters)
        with self.acting_as(self.sid), mock.patch.object(Transaction, 'execute', reject_receipt):
            result = self.hub.handoff(closing['request_id'], outcome='no-durable-update', detail='Unchanged',
                                      experience_file=self.file())
        self.assertEqual(result['closure_state'], 'acknowledged')
        self.assertEqual(result['capture']['status'], 'invalid')
        self.assertEqual(self.experience.page(self.pid)['total'], 0)

    def test_contract_rejects_duplicate_unknown_and_nonfinite_fields(self):
        from lib.control.session_experience import decode_report
        for raw in [b'{"contract":"asha.session-experience.v1","contract":"other"}',
                    json.dumps(dict(report(), contract='v2')).encode(),
                    json.dumps(dict(report(), actor='chair')).encode(),
                    json.dumps(dict(report(), outcome=float('nan'))).encode()]:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                decode_report(raw)

    def test_bounds_and_reference_integrity_refuse_without_truncation(self):
        from lib.control.session_experience import decode_report
        for change in [{'summary': 'x' * 17000}, {'observations': report()['observations'] * 4},
                       {'evidence': report()['evidence'] * 5}, {'evidence': []},
                       {'assessment': 'none-observed'}]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                decode_report(json.dumps(dict(report(), **change)).encode())

    def test_durable_retry_uses_controller_identity_and_no_duplicate_review(self):
        key = str(uuid.uuid4())
        first = self.capture(key=key)
        second = self.capture(key=key)
        self.assertEqual(first, second)
        saved = self.experience.show(first['report_id'])
        self.assertEqual(saved['session_id'], self.sid)
        self.assertEqual(saved['project_id'], self.pid)
        self.assertEqual(saved['body']['evidence'][0]['kind'], 'agent-attestation')
        from lib.control.session_experience import Experiences
        from lib.control.session_hub import Hub
        reopened = Experiences(Hub(self.config, env=self.env, tmux=self.tmux))
        self.assertEqual(len(reopened.page(self.pid)['rows']), 1)
        self.assertEqual(len(saved['reviews']), 1)
        self.assertEqual(saved['reviews'][0]['status'], 'disabled')
        changed = report(); changed['summary'] = 'Changed'
        self.assertEqual(self.capture(changed, key=key)['status'], 'invalid')
        self.assertEqual(reopened.show(first['report_id'])['body']['summary'], report()['summary'])

    def test_invalid_capture_never_blocks_no_update_handoff(self):
        closing = self.hub.close(self.sid)['closure']
        path = self.project / 'oversized'; path.write_bytes(b'x' * 16385)
        before = self.digests()
        with self.acting_as(self.sid):
            result = self.hub.handoff(closing['request_id'], outcome='no-durable-update', detail='Nothing binding',
                                      experience_file=str(path))
        self.assertEqual(result['capture']['status'], 'invalid')
        self.assertEqual(result['closure_state'], 'acknowledged')
        self.assertEqual(self.hub.close(self.sid)['closure']['state'], 'completed')
        self.assertEqual(self.digests(), before)
        self.assertFalse(result['git_invoked'])

    def test_missing_is_distinct_from_none_and_disabled(self):
        close = self.hub.close(self.sid)['closure']
        self.assertTrue(close['capture']['requested'])
        with self.acting_as(self.sid):
            result = self.hub.handoff(close['request_id'], outcome='no-durable-update', detail='Unchanged')
        self.assertEqual(result['capture']['status'], 'missing')
        self.assertEqual(self.experience.page(self.pid)['rows'], [])

    def test_capture_survives_memory_cas_failure_and_retry(self):
        close = self.hub.close(self.sid)['closure']
        active, decisions = self.drafts()
        path = self.file()
        with self.acting_as(self.sid):
            with self.assertRaises(StoreError):
                self.hub.handoff(close['request_id'], active_file=active, decisions_file=decisions,
                    expected={}, experience_file=path)
            first = self.experience.page(self.pid)['rows'][0]
            result = self.hub.handoff(close['request_id'], active_file=active, decisions_file=decisions,
                expected=self.digests(), experience_file=path)
        self.assertEqual(result['capture']['report_id'], first['report_id'])
        self.assertEqual(len(self.experience.page(self.pid)['rows']), 1)
        self.assertEqual(result['closure_state'], 'acknowledged')

    def test_unsafe_files_and_known_secrets_are_omitted_without_echo(self):
        ordinary = self.project / 'good'; ordinary.write_text(json.dumps(report()))
        link = self.project / 'link'; link.symlink_to(ordinary)
        fifo = self.project / 'fifo'; os.mkfifo(fifo)
        secret = report(); secret['summary'] = 'api_key=fixture-private-value'
        for path in [str(link), str(fifo), self.file(secret)]:
            with self.acting_as(self.sid):
                result = self.hub.report(state='finished', body='Done', experience_file=path, key=str(uuid.uuid4()))
            self.assertEqual(result['capture']['status'], 'invalid')
            self.assertNotIn('fixture-private-value', json.dumps(result))
        self.assertEqual(self.experience.page(self.pid)['rows'], [])

    def test_reference_scope_and_correction_preserve_lineage(self):
        first = self.capture()
        changed = report(); changed['summary'] = 'Corrected observation'
        second = self.capture(changed, supersedes=first['report_id'])
        saved = self.experience.show(second['report_id'])
        self.assertEqual(saved['origin_report_id'], first['report_id'])
        self.assertEqual(saved['supersedes'], first['report_id'])
        with self.acting_as(self.sid):
            attached = self.hub.report(state='finished', body='Done', experience_ref=second['report_id'], key=str(uuid.uuid4()))
        self.assertEqual(attached['capture']['report_id'], second['report_id'])
        self.hub.close(self.sid, force=True)
        with self.acting_as(self.sid), self.assertRaises(StoreError):
            self.hub.report(state='finished', body='Late', experience_file=self.file(), key=str(uuid.uuid4()))

    def test_silence_prevents_reading_new_content_and_policy_defaults_off(self):
        self.experience.set_policy(str(self.project), 'off', expected_revision=1)
        self.assertEqual(self.capture()['status'], 'disabled')
        self.experience.set_policy(str(self.project), 'capture', expected_revision=2)
        marker = self.project / 'Work/markers/silence'; marker.parent.mkdir(parents=True, exist_ok=True); marker.touch()
        with mock.patch('lib.control.session_experience.read_report', side_effect=AssertionError('read during silence')):
            self.assertEqual(self.capture()['status'], 'disabled')
        self.assertEqual(self.experience.page(self.pid)['rows'], [])

    def test_worker_cannot_change_policy(self):
        from lib.control.session_experience import Experiences
        from lib.control.session_hub import Hub
        worker = Hub(self.config, env=dict(self.env, ASHA_HUB_SESSION_ID=self.sid, ASHA_SESSION_PROFILE='worker'), tmux=self.tmux)
        with self.assertRaises(StoreError):
            Experiences(worker).set_policy(str(self.project), 'review', expected_revision=1)

    def test_guidance_feedback_cannot_invent_exposure(self):
        value = report(); value['guidance_feedback'] = [{'id': 'invented', 'version': '0' * 64,
            'use': 'applied', 'evidence_ids': ['check']}]
        self.assertEqual(self.capture(value)['status'], 'invalid')

    def test_none_observed_is_explicit_and_paginated(self):
        result = self.capture(report('none-observed'))
        self.assertEqual(result['status'], 'none-observed')
        self.capture(report('insufficient-evidence'))
        page = self.experience.page(self.pid, limit=1)
        self.assertFalse(page['complete'])
        self.assertIsNotNone(page['next_offset'])
        self.assertTrue(self.experience.page(self.pid, offset=page['next_offset'])['complete'])
