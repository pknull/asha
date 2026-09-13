"""Independent guidance custody regressions; no native backend is launched."""
import json
import unittest
import uuid
from pathlib import Path
from unittest import mock

from tests.python.test_control_session_closure import ClosureFixture
from lib.control.session_store import SessionStore
from lib.control.store import StoreError
from lib.control import sessions
import learnings_manager as lm

ACTION = 'Compare the exact current publication baseline.'


class GuidanceReview(ClosureFixture):
    def setUp(self):
        super().setUp()
        self.enterContext(mock.patch.object(lm, 'learnings_dir', return_value=self.root / 'learning-fixture'))
        lm.save(lm.Learning('review-rule', 'Before publishing', ACTION, state='active'), project_dir=self.project)

    def test_rejected_message_cannot_become_a_guidance_supply_receipt(self):
        sid = self.launch()['session_id']
        key = str(uuid.uuid4())
        original = self.hub.send(sid, 'Continue the original work', key=key)
        with self.assertRaises(StoreError):
            self.hub.send(sid, 'Continue the original work', key=key, learning_ids=['review-rule'])
        with self.acting_as(sid):
            self.hub.acknowledge(original['message_id'])
        self.assertFalse(any(item['status'] == 'supplied' for item in self.hub.show(sid)['guidance']))

    def test_structured_resume_delivers_explicit_learning_selection(self):
        sid = self.launch(transport='structured')['session_id']
        with SessionStore(self.config) as store:
            with store.db.transaction(write=True) as c:
                c.execute("UPDATE managed_sessions SET state='stopped' WHERE session_id=?", (sid,))
            expected = store.recovery_digest(store.get(sid))
        self.hub.resume(sid, prompt='Continue with the selected rule', expected_digest=expected,
                        learning_ids=['review-rule'])
        with SessionStore(self.config) as store, store.db.transaction() as c:
            message = c.execute('SELECT body FROM session_messages WHERE session_id=? AND delivery_key=?',
                                (sid, 'recovery:' + expected)).fetchone()
        self.assertIn(ACTION, message['body'])

    def test_old_structured_session_acknowledgement_does_not_require_guidance_table(self):
        sid = self.launch(transport='structured')['session_id']
        with self.hub.database() as db, db.transaction(write=True) as c:
            c.execute('DROP TABLE hub_guidance_exposures')
        # Direct helper seam: a full run_turn probe cannot bind IPC in this sandbox.
        # sessions.run_turn calls this helper after native-input-acknowledged.
        from lib.control.session_guidance import supplied
        supplied(self.hub, self.hub.get(sid), 'opening')

    def test_retired_queued_guidance_is_not_supplied_as_active(self):
        sid = self.launch()['session_id']
        original = self.hub.send(sid, 'Continue with selected guidance', key=str(uuid.uuid4()),
                                 learning_ids=['review-rule'])
        lm.retire('review-rule', 'The rule was superseded before delivery.', project_dir=self.project)
        messages = self.hub.messages(sid)
        self.assertNotIn(ACTION, json.dumps(messages))
