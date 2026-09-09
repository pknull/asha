"""Installed Codex 0.153.4 app-server wire contract; no model calls."""
import json
import unittest
from unittest import mock
from pathlib import Path

from lib.control.store import StoreError


class CodexProtocolTests(unittest.TestCase):
    def actor_request(self, p, **params):
        return p.feed({'id': 42, 'method': 'item/tool/call', 'params': {
            'threadId': 'thread-1', 'turnId': 'turn-1', 'callId': 'call-1',
            'tool': 'asha_control', 'arguments': {'operation': 'ask', 'question': 'Q'}, **params}})

    def test_hosted_tool_handshake_and_response_without_permission_requests(self):
        actor = mock.Mock()
        actor.poll.return_value = []
        p = self.protocol(actor=actor)
        start, _ = self.ready(p)
        self.assertEqual(start['params']['dynamicTools'][0]['name'], 'asha_control')
        self.actor_request(p)
        self.actor_request(p)
        actor.submit.assert_called_once()
        reply = {'success': True, 'contentItems': [{'type': 'inputText', 'text': 'retained'}]}
        actor.poll.return_value = [('codex:42', reply)]
        p.poll()
        self.assertIn('codex:42', p.actor_requests)
        self.assertEqual(self.drain(p), [{'id': 42, 'result': reply}])
        self.assertFalse(p.actor_requests)
        p.feed({'method': 'serverRequest/resolved', 'params': {'threadId': 'thread-1', 'requestId': 42}})

    def test_hosted_tool_scope_and_terminal_pending_are_refused(self):
        for params in ({'threadId': 'foreign'}, {'turnId': 'foreign'}, {'tool': 'shell'},
                       {'namespace': 'foreign'}, {'callId': ''}):
            p = self.protocol(actor=mock.Mock()); self.ready(p)
            with self.assertRaises(StoreError):
                self.actor_request(p, **params)
            p.actor.submit.assert_not_called()
        p = self.protocol(actor=mock.Mock()); self.ready(p)
        self.actor_request(p)
        with self.assertRaisesRegex(StoreError, 'pending'):
            self.notify(p, 'turn/completed', turn={'id': 'turn-1', 'status': 'completed'})
        with self.assertRaisesRegex(StoreError, 'changed'):
            self.actor_request(p, arguments={'operation': 'inspect'})

    def test_hosted_tools_resume_uses_retained_native_definitions(self):
        p = self.protocol(actor=mock.Mock(), native_id='thread-1')
        start, _ = self.ready(p)
        self.assertNotIn('dynamicTools', start['params'])
        self.actor_request(p)
        p.actor.submit.assert_called_once()

    def test_hosted_tool_capacity_refusal_does_not_kill_turn(self):
        actor = mock.Mock()
        actor.submit.return_value = False
        p = self.protocol(actor=actor); self.ready(p)
        self.actor_request(p)
        frame = self.drain(p)[0]
        self.assertFalse(frame['result']['success'])
        self.assertIn('not executed', frame['result']['contentItems'][0]['text'])
        self.assertFalse(p.terminal)
        self.assertFalse(p.actor_requests)

    def protocol(self, **kwargs):
        from lib.control.codex_protocol import CodexProtocol
        return CodexProtocol("Assignment", cwd="/tmp", message_id="asha-message", **kwargs)

    def drain(self, protocol):
        data = bytearray()
        while protocol.outbound:
            chunk = protocol.chunk()
            data.extend(chunk)
            protocol.advance(len(chunk))
        return [json.loads(line) for line in data.splitlines()]

    def ready(self, protocol):
        init = self.drain(protocol)[0]
        protocol.feed({"id": init["id"], "result": {"userAgent": "fixture"}})
        frames = self.drain(protocol)
        self.assertEqual(frames[0]["method"], "initialized")
        request = frames[1]
        events = protocol.feed({"id": request["id"], "result": {"thread": {"id": "thread-1"},
            "cwd": "/tmp", "approvalPolicy": "untrusted", "approvalsReviewer": "user",
            "sandbox": {"type": "workspaceWrite", "networkAccess": False, "writableRoots": []}}})
        self.assertEqual(events, [("initialized", {"native_id": "thread-1"})])
        turn = self.drain(protocol)[0]
        protocol.feed({"id": turn["id"], "result": {"turn": {"id": "turn-1", "status": "inProgress", "items": []}}})
        return request, turn

    def notify(self, protocol, method, **params):
        return protocol.feed({"method": method, "params": {"threadId": "thread-1", "turnId": "turn-1", **params}})

    def test_handshake_does_not_release_input_before_thread_identity(self):
        p = self.protocol()
        self.assertEqual(p.feed({"method": "remoteControl/status/changed", "params": {"status": "disabled"}}), [])
        self.assertTrue(p.input_not_submitted)
        start, turn = self.ready(p)
        self.assertEqual(start["method"], "thread/start")
        self.assertEqual(start["params"]["approvalPolicy"], "untrusted")
        self.assertEqual(start["params"]["sandbox"], "workspace-write")
        self.assertEqual(turn["params"]["clientUserMessageId"], "asha-message")
        self.assertFalse(p.input_not_submitted)

    def test_completed_command_retains_native_failure_and_bounded_diagnostics(self):
        p = self.protocol(); self.ready(p)
        diagnostic = 'session request transport unavailable: Operation not permitted\n'
        events = self.notify(p, 'item/completed', item={
            'id': 'command-1', 'type': 'commandExecution', 'status': 'failed',
            'exitCode': 2, 'aggregatedOutput': diagnostic})
        detail = events[0][1]
        self.assertEqual(detail['status'], 'failed')
        self.assertEqual(detail['exit_code'], 2)
        self.assertEqual(detail['output'], diagnostic)
        self.assertFalse(detail['output_truncated'])
        self.assertFalse(p.terminal)
        events = self.notify(p, 'item/completed', item={
            'id': 'command-2', 'type': 'commandExecution', 'status': 'completed',
            'exitCode': 0, 'aggregatedOutput': '界' * 20000 + 'terminal diagnosis'})
        detail = events[0][1]
        self.assertTrue(detail['output_truncated'])
        self.assertTrue(detail['output'].endswith('terminal diagnosis'))
        self.assertEqual(detail['output_section'], 'tail')
        self.assertLessEqual(len(json.dumps(detail, ensure_ascii=True).encode()), 128 * 1024)

    def test_malformed_optional_diagnostics_do_not_hide_the_tool_status(self):
        p = self.protocol(); self.ready(p)
        event = self.notify(p, 'item/completed', item={
            'id': 'command-1', 'type': 'commandExecution', 'status': 'failed',
            'exitCode': '2', 'aggregatedOutput': {'unexpected': 'shape'}})[0][1]
        self.assertEqual(event['status'], 'failed')
        self.assertTrue(event['exit_code_unavailable'])
        self.assertTrue(event['output_unavailable'])
        self.assertFalse(p.terminal)

    def test_resume_reapplies_context_and_refuses_identity_change(self):
        p = self.protocol(native_id="thread-1")
        start, _ = self.ready(p)
        self.assertEqual(start["method"], "thread/resume")
        self.assertEqual(start["params"]["cwd"], "/tmp")
        self.assertEqual(start["params"]["threadId"], "thread-1")
        with self.assertRaises(StoreError):
            p.feed({"method": "item/started", "params": {"threadId": "foreign", "turnId": "turn-1", "item": {"type": "userMessage", "id": "x"}}})

    def test_resumed_thread_goal_clear_before_turn_response_is_metadata(self):
        contract = json.loads((Path(__file__).parents[1] / "fixtures/codex-app-server-contract-0.153.4.json").read_text())
        schema = contract["definitions"]["ThreadGoalClearedNotification"]
        self.assertEqual(schema["required"], ["threadId"])
        self.assertNotIn("turnId", schema["properties"])
        p = self.protocol(native_id="thread-1")
        init = self.drain(p)[0]
        p.feed({"id": init["id"], "result": {}})
        resume = self.drain(p)[1]
        p.feed({"id": resume["id"], "result": {"thread": {"id": "thread-1"},
            "cwd": "/tmp", "approvalPolicy": "untrusted", "approvalsReviewer": "user",
            "sandbox": {"type": "workspaceWrite"}}})
        self.drain(p)
        # Captured from Codex0.153.4 after thread/resume, before the turn/start
        # response. Its generated schema requires threadId, not turnId.
        self.assertIsNone(p.turn_id)
        self.assertEqual(p.feed({"method": "thread/goal/cleared", "params": {"threadId": "thread-1"}}), [])
        self.assertFalse(p.terminal)
        for params in ({}, {"threadId": "foreign"}):
            with self.assertRaisesRegex(StoreError, "another thread"):
                p.feed({"method": "thread/goal/cleared", "params": params})

    def test_sandbox_wire_defaults_are_pinned_and_broader_echo_is_refused(self):
        contract = json.loads((Path(__file__).parents[1] / "fixtures/codex-app-server-contract-0.153.4.json").read_text())
        variants = contract["definitions"]["SandboxPolicy"]["oneOf"]
        policy = next(v for v in variants if v["properties"]["type"]["enum"] == ["workspaceWrite"])
        self.assertEqual(policy["required"], ["type"])
        self.assertIs(policy["properties"]["networkAccess"]["default"], False)
        self.assertEqual(policy["properties"]["writableRoots"]["default"], [])
        for extra, accepted in (({}, True), ({"networkAccess": True}, False),
                                ({"networkAccess": 0}, False), ({"writableRoots": ["/elsewhere"]}, False)):
            p = self.protocol()
            init = self.drain(p)[0]
            p.feed({"id": init["id"], "result": {}})
            start = self.drain(p)[1]
            reply = {"id": start["id"], "result": {"thread": {"id": "thread-1"}, "cwd": "/tmp",
                "approvalPolicy": "untrusted", "approvalsReviewer": "user",
                "sandbox": {"type": "workspaceWrite", **extra}}}
            if accepted:
                self.assertEqual(p.feed(reply)[0][0], "initialized")
            else:
                with self.assertRaisesRegex(StoreError, "execution policy"):
                    p.feed(reply)
                self.assertTrue(p.input_not_submitted)

    def test_progress_and_completion_are_distinct_and_scoped(self):
        p = self.protocol(); self.ready(p)
        events = self.notify(p, "item/agentMessage/delta", itemId="item-1", delta="Hello")
        self.assertEqual(events, [("text", {"text": "Hello"})])
        self.assertFalse(p.terminal)
        events = p.feed({"method": "turn/completed", "params": {"threadId": "thread-1", "turn": {"id": "turn-1", "status": "completed", "items": []}}})
        self.assertEqual(events[-1][0], "completed")
        self.assertTrue(p.terminal)
        with self.assertRaises(StoreError):
            self.notify(p, "item/agentMessage/delta", itemId="item-1", delta="late")

    def test_command_approval_is_bound_to_exact_rpc_id_and_written_before_receipt(self):
        opened, submitted, replies = [], [], []
        p = self.protocol(open_request=lambda key, payload: opened.append((key, payload)),
                          poll_responses=lambda: list(replies), submitted=submitted.append)
        self.ready(p)
        frame = {"id": 7, "method": "item/commandExecution/requestApproval", "params": {
            "threadId": "thread-1", "turnId": "turn-1", "itemId": "command-1", "startedAtMs": 0,
            "command": "printf hello", "cwd": "/tmp"}}
        p.feed(frame); p.feed(frame)
        self.assertEqual(len(opened), 1)
        key, payload = opened[0]
        self.assertEqual(payload["params"]["command"], "printf hello")
        replies.append({"request_id": "local-1", "frame": {"id": 7, "result": {"decision": "accept"}}})
        p.poll(); replies.clear()
        self.assertEqual(submitted, [])
        self.assertEqual(self.drain(p), [{"id": 7, "result": {"decision": "accept"}}])
        self.assertEqual(submitted, ["local-1"])
        self.assertNotIn(key, p.requests)
        with self.assertRaises(StoreError):
            p.feed(frame)

    def test_changed_request_or_cross_turn_permission_is_refused(self):
        p = self.protocol(open_request=lambda *args: None); self.ready(p)
        params = {"threadId": "thread-1", "turnId": "turn-1", "itemId": "x", "startedAtMs": 0, "command": "true", "cwd": "/tmp"}
        p.feed({"id": "request", "method": "item/commandExecution/requestApproval", "params": params})
        for change in ({"command": "changed"}, {"turnId": "foreign"}):
            with self.assertRaises(StoreError):
                p.feed({"id": "request", "method": "item/commandExecution/requestApproval", "params": {**params, **change}})

    def test_completion_with_unanswered_request_cannot_succeed(self):
        p = self.protocol(open_request=lambda *args: None); self.ready(p)
        p.feed({"id": 2, "method": "item/tool/requestUserInput", "params": {
            "threadId": "thread-1", "turnId": "turn-1", "itemId": "ask", "isBlocking": True,
            "questions": [{"id": "chapter", "header": "Chapter", "question": "Which?", "options": None}]}})
        with self.assertRaises(StoreError):
            p.feed({"method": "turn/completed", "params": {"threadId": "thread-1", "turn": {"id": "turn-1", "status": "completed", "items": []}}})

    def test_nonblocking_native_questions_have_an_explicit_unsupported_disposition(self):
        opened = []
        p = self.protocol(open_request=lambda *args: opened.append(args)); self.ready(p)
        with self.assertRaisesRegex(StoreError, "nonblocking"):
            p.feed({"id": 2, "method": "item/tool/requestUserInput", "params": {
                "threadId": "thread-1", "turnId": "turn-1", "itemId": "ask", "isBlocking": False,
                "questions": [{"id": "chapter", "header": "Chapter", "question": "Which?"}]}})
        self.assertEqual(opened, [])

    def test_resolution_withdraws_unsent_response_but_partial_write_is_uncertain(self):
        for partial in (False, True):
            cancelled, replies = [], []
            p = self.protocol(open_request=lambda *args: None, cancel_request=lambda key, **evidence: cancelled.append((key, evidence)),
                              poll_responses=lambda: replies, submitted=lambda _: None)
            self.ready(p)
            p.feed({"id": 1, "method": "item/commandExecution/requestApproval", "params": {
                "threadId": "thread-1", "turnId": "turn-1", "itemId": "x", "startedAtMs": 0,
                "command": "true", "cwd": "/tmp"}})
            replies.append({"request_id": "local", "frame": {"id": 1, "result": {"decision": "decline"}}})
            p.poll()
            if partial:
                p.advance(1)
            resolved = {"method": "serverRequest/resolved", "params": {"threadId": "thread-1", "requestId": 1}}
            if partial:
                with self.assertRaisesRegex(StoreError, "during response transmission"):
                    p.feed(resolved)
            else:
                p.feed(resolved)
                self.assertFalse(p.outbound)
                self.assertEqual(p.bytes_queued, 0)
                self.assertFalse(p.requests)
            self.assertEqual(cancelled, [("codex:1", {"response_not_submitted": not partial})])

    def test_file_permission_requires_retained_exact_diff(self):
        opened = []
        p = self.protocol(open_request=lambda *args: opened.append(args)); self.ready(p)
        approval = {"id": 1, "method": "item/fileChange/requestApproval", "params": {
            "threadId": "thread-1", "turnId": "turn-1", "itemId": "file-1", "startedAtMs": 0}}
        with self.assertRaisesRegex(StoreError, "lacks exact"):
            p.feed(approval)
        change = {"id": "file-1", "type": "fileChange", "status": "inProgress", "changes": [
            {"path": "/tmp/chapter.md", "kind": {"type": "add"}, "diff": "Quiet."}]}
        self.notify(p, "item/started", item=change)
        p.feed(approval)
        self.assertEqual(opened[0][1]["item"], change)
        self.notify(p, "item/completed", item={**change, "status": "completed"})
        p.feed(approval)
        self.assertEqual(len(opened), 1)
        self.notify(p, "item/completed", item={**change, "changes": [{**change["changes"][0], "diff": "Different"}]})
        with self.assertRaisesRegex(StoreError, "reused or changed"):
            p.feed(approval)
        with self.assertRaisesRegex(StoreError, "session-wide"):
            p.feed({**approval, "id": 2, "params": {**approval["params"], "grantRoot": "/tmp"}})

    def test_typed_error_notifications_are_scoped_and_retry_does_not_park(self):
        p = self.protocol(); self.ready(p)
        error = {"codexErrorInfo": "usageLimitExceeded", "message": "diagnostic"}
        retry = self.notify(p, "error", error=error, willRetry=True)
        self.assertEqual([kind for kind, _ in retry], ["progress"])
        stopped = self.notify(p, "error", error=error, willRetry=False)
        self.assertEqual(stopped[-1][0], "provider-status")
        self.assertEqual(stopped[-1][1]["source"], "turn_error")
        self.assertEqual(stopped[-1][1]["reason"], "rate_limit")
        with self.assertRaisesRegex(StoreError, "another turn"):
            p.feed({"method": "error", "params": {"threadId": "thread-1", "error": error, "willRetry": False}})

    def test_large_file_work_retains_tool_progress_and_bounded_review_cache(self):
        from lib.control.codex_protocol import MAX_REVIEW_BYTES, MAX_ITEM_CACHE_BYTES
        opened = []
        p = self.protocol(open_request=lambda *args: opened.append(args)); self.ready(p)
        def item(i, length):
            return {"id": str(i), "type": "fileChange", "changes": [
                {"path": "/tmp/chapter.md", "kind": {"type": "add"}, "diff": "x" * length}]}
        # A normal 50KiB patch is reviewable, well above the old 32KiB cap.
        self.notify(p, "item/started", item=item(1, 50000))
        p.feed({"id": 1, "method": "item/fileChange/requestApproval", "params": {
            "threadId": "thread-1", "turnId": "turn-1", "itemId": "1", "startedAtMs": 0}})
        self.assertEqual(len(opened[0][1]["item"]["changes"][0]["diff"]), 50000)
        events = self.notify(p, "item/started", item=item(2, MAX_REVIEW_BYTES + 1))
        self.assertEqual(events[0][0], "tool")
        self.assertNotIn("2", p.items)
        for index in range(3, 20):
            self.notify(p, "item/started", item=item(index, 200000))
        self.assertLessEqual(sum(len(json.dumps(v, ensure_ascii=True).encode()) for v in p.items.values()), MAX_ITEM_CACHE_BYTES)
        self.assertNotIn("1", p.items)
        p.feed({"id": 1, "method": "item/fileChange/requestApproval", "params": {
            "threadId": "thread-1", "turnId": "turn-1", "itemId": "1", "startedAtMs": 0}})
        self.assertEqual(len(opened), 1)
        self.assertEqual(p.item_bytes, sum(p.item_sizes.values()))
        self.assertEqual(p.item_bytes, sum(len(json.dumps(v, ensure_ascii=True).encode()) for v in p.items.values()))

    def test_native_request_aggregate_is_bounded_before_new_review_is_opened(self):
        from lib.control.codex_protocol import MAX_REVIEW_TURN_BYTES
        opened = []
        p = self.protocol(open_request=lambda *args: opened.append(args)); self.ready(p)
        for index in range(32):
            item = {"id": str(index), "type": "fileChange", "changes": [
                {"path": "/tmp/chapter.md", "kind": {"type": "add"}, "diff": "x" * 200000}]}
            self.notify(p, "item/started", item=item)
            approval = {"id": index, "method": "item/fileChange/requestApproval", "params": {
                "threadId": "thread-1", "turnId": "turn-1", "itemId": str(index), "startedAtMs": 0}}
            if p.seen_bytes + 201000 > MAX_REVIEW_TURN_BYTES:
                before = len(opened)
                with self.assertRaisesRegex(StoreError, "per-turn review"):
                    p.feed(approval)
                self.assertEqual(len(opened), before)
                break
            p.feed(approval)
        else:
            self.fail("aggregate review cap was not enforced")
        self.assertLessEqual(p.seen_bytes, MAX_REVIEW_TURN_BYTES)

    def test_interrupted_turn_closes_pending_request_and_preserves_typed_quota(self):
        cancelled = []
        p = self.protocol(open_request=lambda *args: None,
                          cancel_request=lambda key, **evidence: cancelled.append((key, evidence)))
        self.ready(p)
        p.feed({"id": 1, "method": "item/commandExecution/requestApproval", "params": {
            "threadId": "thread-1", "turnId": "turn-1", "itemId": "x", "startedAtMs": 0,
            "command": "true", "cwd": "/tmp"}})
        events = p.feed({"method": "turn/completed", "params": {"threadId": "thread-1", "turn": {
            "id": "turn-1", "status": "interrupted", "error": {"codexErrorInfo": "usageLimitExceeded"}}}})
        self.assertEqual(events[0][1]["reason"], "rate_limit")
        self.assertEqual(events[-1][0], "failed")
        self.assertEqual(cancelled, [("codex:1", {"response_not_submitted": True})])
        self.assertFalse(p.requests)

    def test_terminal_error_is_typed_but_diagnostic_text_cannot_set_quota(self):
        p = self.protocol(); self.ready(p)
        events = p.feed({"method": "turn/completed", "params": {"threadId": "thread-1", "turn": {
            "id": "turn-1", "status": "failed", "items": [], "error": {"message": "try later", "codexErrorInfo": "usageLimitExceeded"}}}})
        self.assertEqual(events[0][0], "provider-status")
        self.assertEqual(events[0][1]["reason"], "rate_limit")
        self.assertEqual(events[-1][0], "failed")

    def test_early_rejection_has_no_input_submission(self):
        p = self.protocol(); init = self.drain(p)[0]
        with self.assertRaises(StoreError):
            p.feed({"id": init["id"], "error": {"code": -32600, "message": "rejected"}})
        self.assertTrue(p.input_not_submitted)
