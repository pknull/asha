import json
import unittest

from lib.control.claude_protocol import ClaudeProtocol
from lib.control.store import StoreError


class ClaudeProtocolTests(unittest.TestCase):
    def protocol(self, **kwargs):
        result = ClaudeProtocol("Assignment", message_id="message-1", **kwargs)
        while result.outbound:
            result.advance(len(result.chunk()))
        result.feed({"type": "control_response", "response": {"request_id": result.initialize_id, "subtype": "success"}})
        return result

    def test_initialize_precedes_user_input_and_ack_is_not_consumption(self):
        protocol = ClaudeProtocol("Assignment", message_id="message-1")
        self.assertEqual(json.loads(protocol.outbound[0]["raw"])["request"]["subtype"], "initialize")
        with self.assertRaises(StoreError):
            protocol.feed({"type": "result", "subtype": "success"})
        protocol = self.protocol()
        self.assertEqual(json.loads(protocol.outbound[0]["raw"])["message"]["content"], "Assignment")
        events = protocol.feed({"type": "user", "uuid": "message-1"})
        self.assertEqual(events[0][0], "progress")

    def test_startup_hook_events_do_not_release_the_assignment(self):
        protocol = ClaudeProtocol("Assignment")
        for subtype in ("hook_started", "hook_progress", "hook_response"):
            self.assertEqual(protocol.feed({"type": "system", "subtype": subtype})[0][0], "progress")
        self.assertFalse(protocol.initialized)
        self.assertEqual(len(protocol.outbound), 1)
        self.assertEqual(json.loads(protocol.outbound[0]["raw"])["type"], "control_request")
        with self.assertRaises(StoreError):
            protocol.feed({"type": "assistant", "message": {"content": []}})

    def test_duplicate_permission_does_not_create_two_requests(self):
        calls = []
        protocol = self.protocol(open_request=lambda *args: calls.append(args))
        frame = {"type": "control_request", "request_id": "native-1", "request": {"subtype": "can_use_tool", "tool_name": "Bash", "input": {}}}
        protocol.feed(frame)
        protocol.feed(frame)
        self.assertEqual(len(calls), 1)
        with self.assertRaises(StoreError):
            protocol.feed({**frame, "request": {**frame["request"], "input": {"changed": True}}})

    def test_cancel_withdraws_unsent_response_and_keeps_partial_frame_intact(self):
        for partial in (False, True):
            sent, cancelled = [], []
            frame = {"type": "control_response", "response": {"request_id": "native-1", "subtype": "success", "response": {"behavior": "deny"}}}
            queued = [{"request_id": "local-1", "frame": frame}]
            protocol = self.protocol(open_request=lambda *_: None, cancel_request=cancelled.append,
                poll_responses=lambda: [queued.pop()] if queued else [], submitted=sent.append)
            while protocol.outbound:
                protocol.advance(len(protocol.chunk()))
            protocol.feed({"type": "control_request", "request_id": "native-1", "request": {"subtype": "can_use_tool"}})
            protocol.poll()
            if partial:
                protocol.advance(1)
            protocol.feed({"type": "control_cancel_request", "request_id": "native-1"})
            self.assertEqual(cancelled, ["native-1"])
            self.assertEqual(bool(protocol.outbound), partial)
            while protocol.outbound:
                protocol.advance(len(protocol.chunk()))
            self.assertEqual(sent, ["local-1"] if partial else [])

    def test_native_terminal_waits_for_background_work(self):
        protocol = self.protocol()
        protocol.feed({"type": "system", "subtype": "task_started", "task_type": "local_agent", "task_id": "child-1"})
        result = {"type": "result", "subtype": "success"}
        self.assertEqual(protocol.feed(result)[0][0], "progress")
        self.assertFalse(protocol.terminal)
        protocol.feed({"type": "system", "subtype": "task_notification", "task_id": "child-1", "status": "completed"})
        self.assertEqual(protocol.feed(result)[0][0], "completed")
        self.assertTrue(protocol.terminal)
        self.assertEqual(protocol.feed({"type": "system", "subtype": "session_state_changed"}), [])
        self.assertEqual(protocol.feed({"type": "command_lifecycle"}), [])
        for subtype in ("hook_started", "hook_progress", "hook_response"):
            self.assertEqual(protocol.feed({"type": "system", "subtype": subtype}), [])
        with self.assertRaises(StoreError):
            protocol.feed({"type": "assistant", "message": {"content": []}})

    def test_terminal_with_pending_permission_is_not_success(self):
        protocol = self.protocol(open_request=lambda *_: None)
        protocol.feed({"type": "control_request", "request_id": "native-1", "request": {"subtype": "can_use_tool"}})
        with self.assertRaisesRegex(StoreError, "unanswered"):
            protocol.feed({"type": "result", "subtype": "success"})

    def test_malformed_control_metadata_has_a_transport_error(self):
        frames = [
            {"type": "control_cancel_request", "request_id": []},
            {"type": "system", "subtype": "task_started", "task_type": [], "task_id": "child"},
            {"type": "system", "subtype": "task_notification", "status": [], "task_id": "child"},
            {"type": "system", "subtype": "task_notification", "status": "completed", "task_id": []},
            {"type": "control_request", "request_id": "n", "request": {"subtype": "can_use_tool", "input": {"bad": float("nan")}}},
        ]
        for frame in frames:
            with self.subTest(frame=frame), self.assertRaises(StoreError):
                self.protocol(open_request=lambda *_: None).feed(frame)
