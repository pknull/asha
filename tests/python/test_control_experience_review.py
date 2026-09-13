"""Transactional admission, one-turn custody and packet-only native review."""
import json
import threading
import unittest
from unittest import mock
from tests.python.test_control_session_experience import ExperienceFixture, report
from lib.control.session_store import SessionStore


class ReviewTests(ExperienceFixture):
    # Reuse only fixture helpers, not its test methods.
    def setUp(self):
        super().setUp()
        self.experience.set_policy(str(self.project), 'review', expected_revision=1)

    def selected(self):
        saved = self.capture()
        return self.experience.show(saved['report_id'])['reviews'][0]

    def test_unverified_native_backend_defers_without_creating_a_utility(self):
        from lib.control import experience_review as review
        row = self.selected()
        review.reconcile(self.hub)
        saved = self.experience.show(row['report_id'])['reviews'][0]
        self.assertEqual(saved['status'], 'unsupported')
        self.assertIsNone(saved['utility_id'])

    def test_reservation_is_atomic_stable_and_daily_cap_transactional(self):
        from lib.control import experience_review as review
        rows = [self.selected() for _ in range(6)]
        with mock.patch.object(review, 'backend_support', return_value=(True, 'fixture')):
            first = review.reserve(self.hub, rows[0]['review_id'])
            same = review.reserve(self.hub, rows[0]['review_id'])
            self.assertEqual(first['utility_id'], same['utility_id'])
            self.assertEqual(review.reserve(self.hub, rows[1]['review_id'])['status'], 'budget-deferred')
            for row in rows[:5]:
                review_id = row['review_id']
                reserved = review.reserve(self.hub, review_id)
                with self.hub.database() as db, db.transaction(write=True) as c:
                    c.execute("UPDATE hub_experience_reviews SET status='review-failed',finished_at=1 WHERE review_id=?", (review_id,))
            self.assertEqual(review.reserve(self.hub, rows[5]['review_id'])['status'], 'budget-deferred')
            with SessionStore(self.config) as store, store.db.transaction() as c:
                self.assertEqual(c.execute('SELECT COUNT(*) FROM managed_sessions').fetchone()[0], 5)

    def test_packet_binds_frozen_digest_and_rejects_malformed_findings(self):
        from lib.control import experience_review as review
        row = self.selected()
        packet = review.packet(self.hub, row['review_id'])
        self.assertLessEqual(len(packet.encode()), 65536)
        self.assertIn('UNTRUSTED', packet)
        for raw in [b'{}', b'{"findings":[],"findings":[]}', b'x' * 16385]:
            with self.assertRaises(ValueError):
                review.decode_result(raw, self.experience.show(row['report_id']), '0' * 64)

    def test_claude_restriction_removes_tools_mcp_hooks_and_native_resume(self):
        from lib.control.experience_review import reviewer_argv
        from pathlib import Path
        argv = reviewer_argv(Path(__file__).resolve().parents[2])
        for flag in ['--bare', '--strict-mcp-config', '--disable-slash-commands', '--no-session-persistence']:
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index('--tools') + 1], '')
        self.assertEqual(argv[argv.index('--max-turns') + 1], '1')
        self.assertNotIn('--resume', argv)

    def test_silence_after_reservation_defers_only_owned_review(self):
        from lib.control import experience_review as review
        row = self.selected()
        with mock.patch.object(review, 'backend_support', return_value=(True, 'fixture')):
            reserved = review.reserve(self.hub, row['review_id'])
        marker = self.project / 'Work/markers/silence'; marker.parent.mkdir(parents=True, exist_ok=True); marker.touch()
        review.reconcile(self.hub)
        self.assertEqual(self.experience.show(row['report_id'])['reviews'][0]['status'], 'silence-deferred')
        self.assertEqual(self.hub.get(self.sid)['lifecycle'], 'open')
        with SessionStore(self.config) as store:
            self.assertTrue(store.get(reserved['utility_id'])['stop_requested'])

    def test_review_utilities_cannot_be_resumed_or_receive_followup(self):
        from lib.control import experience_review as review
        from lib.control.store import StoreError
        row = self.selected()
        with mock.patch.object(review, 'backend_support', return_value=(True, 'fixture')):
            reserved = review.reserve(self.hub, row['review_id'])
        with SessionStore(self.config) as store:
            with self.assertRaises(StoreError):
                store.enqueue(reserved['utility_id'], 'Another review', key='again')
            state = store.get(reserved['utility_id'])
            with self.assertRaises(StoreError):
                store.resume(state['session_id'], prompt='again', expected_digest=store.recovery_digest(state))
