import unittest
import uuid
from unittest import mock

from lib.control import tui
from lib.control.session_store import SessionStore
from tests.python import test_control_managed_sessions as fixtures


class ManagedRequestPickerTests(unittest.TestCase):
    setUp = fixtures.SessionTests.setUp

    def requests(self):
        turn = self.store.claim_turn(self.sid, self.generation)
        ids = [str(uuid.uuid4()) for _ in range(3)]
        for index, request_id in enumerate(ids):
            self.store.request(self.sid, turn['turn_id'], 'Question '+str(index), request_id=request_id)
        return ids

    def test_later_requests_can_be_selected_without_resolving_them(self):
        ids = self.requests()
        with mock.patch.object(tui, '_MAX_MODAL_CANDIDATES', 2), \
             mock.patch.object(tui, '_prompt_line', side_effect=['next', ids[2]]) as prompt:
            selected = tui._select_managed_request(None, None, tui.TuiModel([]), self.config)
        self.assertEqual(selected, ids[2])
        self.assertIn('next', [c.value for c in prompt.call_args_list[0].kwargs['candidates']])
        self.assertIn(ids[2], [c.value for c in prompt.call_args_list[1].kwargs['candidates']])
        self.assertEqual([self.store.get_request(r)['state'] for r in ids], ['pending'] * 3)

    def test_empty_partial_page_can_retry_the_same_cursor(self):
        ids = self.requests()
        complete = self.store.current_work(kind='requests')
        partial = {**complete, 'rows': [], 'complete': False, 'next_cursor': None}
        with mock.patch.object(SessionStore, 'current_work', side_effect=[partial, complete]) as read, \
             mock.patch.object(tui, '_prompt_line', side_effect=['retry', None]):
            self.assertIsNone(tui._select_managed_request(None, None, tui.TuiModel([]), self.config))
        self.assertEqual([call.kwargs['after'] for call in read.call_args_list], [None, None])
        self.assertEqual([self.store.get_request(r)['state'] for r in ids], ['pending'] * 3)

    def test_unavailable_navigation_words_do_not_become_request_ids(self):
        ids = self.requests()
        with mock.patch.object(tui, '_prompt_line', side_effect=['next', 'retry', ids[0]]):
            selected = tui._select_managed_request(None, None, tui.TuiModel([]), self.config)
        self.assertEqual(selected, ids[0])
        self.assertEqual([self.store.get_request(r)['state'] for r in ids], ['pending'] * 3)
