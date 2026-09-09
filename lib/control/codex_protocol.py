"""Codex 0.153.4 app-server state machine; no processes or database writes.

Wire names and required identity fields come from the installed CLI's generated
nonexperimental JSON schemas. A turn result is not a consumption receipt.
"""
from __future__ import annotations

from collections import deque
import json
import uuid

from .store import StoreError

REQUEST_METHODS = frozenset({"item/commandExecution/requestApproval", "item/fileChange/requestApproval",
    "item/permissions/requestApproval", "item/tool/requestUserInput"})
MAX_REVIEW_BYTES = 256 * 1024
MAX_ITEM_CACHE_BYTES = 2 * 1024 * 1024
MAX_REVIEW_TURN_BYTES = 4 * 1024 * 1024
METADATA_METHODS = frozenset({"thread/started", "thread/status/changed", "thread/tokenUsage/updated",
    "account/rateLimits/updated", "account/updated", "model/rerouted", "thread/name/updated",
    "thread/settings/updated", "thread/compacted", "skills/changed", "hook/started", "hook/completed",
    "mcpServer/startupStatus/updated", "remoteControl/status/changed", "configWarning", "warning", "deprecationNotice"})


def _text(value, label, limit=512):
    try:
        if not isinstance(value, str) or not value or "\x00" in value or len(value.encode()) > limit:
            raise ValueError()
    except (ValueError, UnicodeError) as exc:
        raise StoreError("invalid Codex " + label) from exc
    return value


def request_key(value):
    if type(value) is int:
        if not 0 <= value <= 9223372036854775807:
            raise StoreError("invalid Codex request ID")
    else:
        _text(value, "request ID", 256)
    return "codex:" + json.dumps(value, ensure_ascii=True, separators=(",", ":"))


def error_status(error, *, interrupted=False):
    """Classify typed errors only. Diagnostic prose never proves quota state."""
    info = error.get("codexErrorInfo") if isinstance(error, dict) else None
    reason = {"usageLimitExceeded": "rate_limit", "rateLimitExceeded": "rate_limit",
        "unauthorized": "authentication_failed", "sessionBudgetExceeded": "native_budget",
        "contextWindowExceeded": "native_budget", "serverOverloaded": "server_error",
        "internalServerError": "server_error", "badRequest": "invalid_request"}.get(info) if isinstance(info, str) else None
    if (isinstance(info, dict) and len(info) == 1 and next(iter(info)) in {
            "httpConnectionFailed", "responseStreamConnectionFailed", "responseStreamDisconnected", "responseTooManyFailedAttempts"}):
        detail = next(iter(info.values()))
        code = detail.get("httpStatusCode") if isinstance(detail, dict) else None
        if type(code) is int:
            reason = {401: "authentication_failed", 402: "billing_error", 429: "rate_limit"}.get(
                code, "server_error" if 500 <= code <= 599 else None)
    return {"contract": "asha.provider-status.v1", "provider": "codex", "source": "turn_status",
        "status": "rejected", "reason": reason or ("cancelled" if interrupted else "unknown"),
        "reset_at": None, "window": None}


class CodexProtocol:
    def __init__(self, prompt, *, cwd, native_id=None, message_id=None,
                 open_request=None, cancel_request=None, poll_responses=None, submitted=None, actor=None):
        self.prompt = _text(prompt, "prompt", 256 * 1024)
        self.cwd = _text(cwd, "working directory", 4096)
        self.native_id = _text(native_id, "thread ID") if native_id is not None else None
        self.message_id = message_id or str(uuid.uuid4())
        self.open_request, self.cancel_request = open_request, cancel_request
        self.poll_responses, self.submitted = poll_responses, submitted
        self.actor = actor
        self.actor_requests, self.actor_seen = {}, {}
        self.initialized = self.terminal = False
        self.input_not_submitted = True
        self.input_written = False
        self.turn_id = None
        self.requests, self.seen, self.pending, self.items = {}, {}, {}, {}
        self.item_sizes = {}
        self.item_bytes = self.seen_bytes = 0
        self.outbound = deque()
        self.bytes_queued = 0
        self.call("initialize", {"clientInfo": {"name": "asha", "title": "Asha", "version": "1"},
                                 "capabilities": {"experimentalApi": actor is not None}})

    def call(self, method, params):
        rid = "asha-" + str(uuid.uuid4())
        self.pending[rid] = method
        self.queue({"id": rid, "method": method, "params": params}, input_frame=method == "turn/start")

    def queue(self, frame, local_id=None, *, input_frame=False, actor_key=None):
        raw = (json.dumps(frame, ensure_ascii=True, allow_nan=False, separators=(",", ":")) + "\n").encode()
        if self.bytes_queued + len(raw) > 1024 * 1024:
            raise StoreError("Codex control output queue overflow")
        self.outbound.append({"raw": raw, "offset": 0, "local_id": local_id,
            "key": request_key(frame["id"]) if local_id is not None else None,
            "actor_key": actor_key, "input": input_frame})
        self.bytes_queued += len(raw)

    def chunk(self):
        if not self.outbound:
            return b""
        item = self.outbound[0]
        return item["raw"][item["offset"]:item["offset"] + 4096]

    def advance(self, count):
        if type(count) is not int or not self.outbound or not 0 < count <= len(self.chunk()):
            raise StoreError("invalid Codex output write count")
        item = self.outbound[0]
        if item["input"]:
            self.input_not_submitted = False
        item["offset"] += count
        self.bytes_queued -= count
        if item["offset"] == len(item["raw"]):
            self.outbound.popleft()
            if item["input"]:
                self.input_written = True
            if item["local_id"] is not None:
                self.submitted(item["local_id"])
                self.requests.pop(item["key"])
            if item["actor_key"] is not None:
                self.actor_requests.pop(item["actor_key"])

    def poll(self):
        if self.terminal:
            return
        if self.actor is not None:
            for key, result in self.actor.poll():
                if key not in self.actor_requests or any(item['actor_key'] == key for item in self.outbound):
                    raise StoreError('actor response does not match a pending request')
                self.queue({'id': self.actor_requests[key]['id'], 'result': result}, actor_key=key)
        for reply in self.poll_responses() if self.requests and self.poll_responses is not None else ():
            frame = reply.get("frame", {})
            key = request_key(frame.get("id"))
            if key not in self.requests or set(frame) != {"id", "result"} or not isinstance(frame["result"], dict):
                raise StoreError("Codex reply does not match a pending native request")
            if any(item["key"] == key for item in self.outbound):
                raise StoreError("Codex reply already queued")
            self._check_reply(self.requests[key], frame["result"])
            self.queue(frame, reply["request_id"])

    @staticmethod
    def _check_reply(request, result):
        if not isinstance(result, dict):
            raise StoreError("Codex native reply must be an object")
        method = request["method"]
        if method in {"item/commandExecution/requestApproval", "item/fileChange/requestApproval"}:
            if set(result) != {"decision"} or result["decision"] not in ("accept", "decline", "cancel"):
                raise StoreError("Codex approval must be a decision for this invocation only")
        elif method == "item/permissions/requestApproval":
            if (set(result) != {"permissions", "scope"} or result["scope"] != "turn"
                    or result["permissions"] not in ({}, request["params"]["permissions"])):
                raise StoreError("Codex permission grant exceeds the exact request or turn")
        else:
            expected = {q["id"] for q in request["params"]["questions"]}
            answers = result.get("answers")
            if set(result) != {"answers"} or not isinstance(answers, dict) or set(answers) != expected:
                raise StoreError("Codex answers do not match the exact native questions")
            for answer in answers.values():
                if (not isinstance(answer, dict) or set(answer) != {"answers"}
                        or not isinstance(answer["answers"], list) or not 1 <= len(answer["answers"]) <= 16):
                    raise StoreError("invalid Codex native answer")
                for text in answer["answers"]:
                    _text(text, "native answer", 4096)

    def _turn(self, value):
        if not isinstance(value, dict):
            raise StoreError("invalid Codex turn")
        tid = _text(value.get("id"), "turn ID")
        if not self.input_written or self.turn_id is not None and tid != self.turn_id:
            raise StoreError("Codex turn identity does not match the submitted input")
        self.turn_id = tid

    def _scope(self, params, *, turn=True):
        if not isinstance(params, dict) or self.native_id is None or params.get("threadId") != self.native_id:
            raise StoreError("Codex frame belongs to another thread")
        if turn and (self.turn_id is None or params.get("turnId") != self.turn_id):
            raise StoreError("Codex frame belongs to another turn")

    def _response(self, value):
        rid = value.get("id")
        request_key(rid)
        method = self.pending.pop(rid, None)
        if method is None or ("result" in value) == ("error" in value):
            raise StoreError("unexpected Codex response")
        if "error" in value:
            raise StoreError("Codex rejected " + method + "; inspect native provider diagnostics")
        result = value["result"]
        if not isinstance(result, dict):
            raise StoreError("invalid Codex response body")
        if method == "initialize":
            self.initialized = True
            self.queue({"method": "initialized", "params": {}})
            params = {"cwd": self.cwd, "approvalPolicy": "untrusted", "approvalsReviewer": "user",
                "sandbox": "workspace-write", "config": {"sandbox_workspace_write.network_access": False,
                "sandbox_workspace_write.writable_roots": [self.cwd]}}
            if self.native_id is not None:
                params["threadId"] = self.native_id
            elif self.actor is not None:
                from .codex_actor import TOOL
                params['dynamicTools'] = [TOOL]
            self.call("thread/resume" if self.native_id is not None else "thread/start", params)
            return []
        if method in {"thread/start", "thread/resume"}:
            thread = result.get("thread")
            if not isinstance(thread, dict):
                raise StoreError("Codex thread response lacks identity")
            tid = _text(thread.get("id"), "thread ID")
            if self.native_id is not None and tid != self.native_id:
                raise StoreError("Codex resumed a different thread")
            sandbox = result.get("sandbox")
            # Installed SandboxPolicy defines omitted networkAccess=false and
            # writableRoots=[] (cwd is implicit). These are wire defaults,
            # not guesses about absent attestation. Reject contrary values.
            if (result.get("cwd") != self.cwd or result.get("approvalPolicy") != "untrusted"
                    or result.get("approvalsReviewer") != "user" or not isinstance(sandbox, dict)
                    or sandbox.get("type") != "workspaceWrite" or sandbox.get("networkAccess", False) is not False
                    or not isinstance(sandbox.get("writableRoots", []), list)
                    or any(root != self.cwd for root in sandbox.get("writableRoots", []))):
                raise StoreError("Codex did not retain the requested working directory and execution policy")
            self.native_id = tid
            self.call("turn/start", {"threadId": tid, "clientUserMessageId": self.message_id,
                "input": [{"type": "text", "text": self.prompt}], "cwd": self.cwd,
                "approvalPolicy": "untrusted", "approvalsReviewer": "user",
                "sandboxPolicy": {"type": "workspaceWrite", "writableRoots": [self.cwd], "networkAccess": False}})
            return [("initialized", {"native_id": tid})]
        self._turn(result.get("turn"))
        return [] if self.terminal else [("progress", {"subtype": "native-input-acknowledged", "message_id": self.message_id})]

    def _request(self, value):
        method, params = value["method"], value.get("params")
        if method == 'item/tool/call':
            return self._actor_request(value)
        if method not in REQUEST_METHODS or self.open_request is None:
            raise StoreError("unsupported Codex native request: " + str(method)[:100])
        self._scope(params)
        key = request_key(value.get("id"))
        if key in self.actor_seen:
            raise StoreError('Codex request ID changed type')
        _text(params.get("itemId"), "request item ID")
        payload = {"protocol": "codex-app-server-v2", "rpc_id": value["id"], "method": method, "params": params}
        if method == "item/fileChange/requestApproval":
            item = self.items.get(params["itemId"])
            if item is None and key in self.seen:
                item = json.loads(self.seen[key]).get("item")
            if params.get("grantRoot") is not None or item is None or item.get("type") != "fileChange":
                raise StoreError("unsupported Codex file approval: lacks exact retained changes or requests a session-wide root grant")
            payload["item"] = item
            if key in self.seen:
                retained = json.loads(self.seen[key]).get("item")
                if retained and retained.get("changes") == item.get("changes"):
                    # Completion changes item status, not the approved diff.
                    # A retry retains the exact original review snapshot.
                    payload["item"] = retained
        from .codex_native import describe
        describe(payload)
        canonical = json.dumps(payload, sort_keys=True, ensure_ascii=True, allow_nan=False)
        if len(canonical.encode()) > MAX_REVIEW_BYTES:
            raise StoreError("Codex native request exceeds retained review size")
        if key in self.seen:
            if self.seen[key] != canonical or key not in self.requests:
                raise StoreError("Codex request ID was reused or changed")
            return []
        size = len(canonical.encode())
        if len(self.seen) + len(self.actor_seen) >= 128 or self.seen_bytes + size > MAX_REVIEW_TURN_BYTES:
            raise StoreError("unsupported Codex native request: per-turn review count or byte limit exceeded")
        payload = json.loads(canonical)
        self.open_request(key, payload)
        self.seen[key] = canonical
        self.seen_bytes += size
        self.requests[key] = payload
        return []

    def _actor_request(self, value):
        params = value.get('params')
        self._scope(params)
        if self.actor is None or params.get('tool') != 'asha_control' or params.get('namespace') is not None:
            raise StoreError('unsupported Codex actor tool')
        call_id = _text(params.get('callId'), 'actor call ID')
        key = request_key(value.get('id'))
        if key in self.seen:
            raise StoreError('Codex request ID changed type')
        from .codex_actor import encode
        canonical = encode(params)
        if key in self.actor_seen:
            if self.actor_seen[key] != canonical or key not in self.actor_requests:
                raise StoreError('Codex actor request changed or reused after reply')
            return []
        if len(self.seen) + len(self.actor_seen) >= 128 or self.seen_bytes + len(canonical) > MAX_REVIEW_TURN_BYTES:
            raise StoreError('Codex per-turn actor count or byte limit exceeded')
        admitted = self.actor.submit(key, call_id, params.get('arguments'))
        self.actor_seen[key] = canonical
        self.seen_bytes += len(canonical)
        self.actor_requests[key] = {'id': value['id'], 'call_id': call_id}
        if admitted is False:
            from .codex_actor import response
            self.queue({'id': value['id'], 'result': response({
                'error': 'actor capacity reached; this call was not executed. Wait for pending calls before retrying.'},
                success=False)}, actor_key=key)
        return [('tool', {'tool_id': call_id, 'name': 'asha_control', 'status': 'inProgress'})]

    def _withdraw(self, key, *, allow_partial=False):
        if self.cancel_request is None:
            raise StoreError("Codex request resolution has no durable handler")
        partial = any(item["key"] == key and item["offset"] for item in self.outbound)
        self.cancel_request(key, response_not_submitted=not partial)
        if partial and not allow_partial:
            raise StoreError("Codex resolved a request during response transmission")
        self.outbound = deque(item for item in self.outbound if item["key"] != key)
        self.bytes_queued = sum(len(item["raw"]) - item["offset"] for item in self.outbound)
        self.requests.pop(key)

    def feed(self, value):
        if not isinstance(value, dict) or value.get("jsonrpc", "2.0") != "2.0":
            raise StoreError("invalid Codex protocol frame")
        if "method" not in value:
            return self._response(value)
        method, params = value["method"], value.get("params", {})
        if not isinstance(method, str) or not isinstance(params, dict):
            raise StoreError("invalid Codex notification")
        if "id" in value:
            if self.terminal:
                raise StoreError("Codex requested work after the terminal event")
            return self._request(value)
        if method == "serverRequest/resolved":
            self._scope(params, turn=False)
            key = request_key(params.get("requestId"))
            if key in self.actor_seen:
                if key in self.actor_requests:
                    raise StoreError('Codex withdrew an actor request before its reply; inspect retained effects')
                return []
            if key not in self.seen:
                raise StoreError("Codex resolved an unknown request")
            if key not in self.requests:
                return []
            self._withdraw(key)
            return []
        if method == "thread/goal/cleared":
            # Codex0.153.4 emits this on resume before turn/start responds.
            # It clears native thread metadata, not an Asha turn or goal.
            self._scope(params, turn=False)
            return []
        if method in METADATA_METHODS:
            if self.native_id is not None and "threadId" in params:
                self._scope(params, turn=False)
            return []
        if not self.initialized or self.terminal:
            raise StoreError("Codex work arrived before initialization or after completion")
        if method in {"turn/started", "turn/completed"}:
            self._scope(params, turn=False)
            turn = params.get("turn")
            self._turn(turn)
            if method == "turn/started":
                if turn.get("status") != "inProgress":
                    raise StoreError("invalid Codex started turn status")
                return [("progress", {"subtype": "turn-started"})]
            if (turn.get("status") not in {"completed", "failed", "interrupted"}
                    or (self.requests or self.actor_requests) and turn["status"] == "completed"):
                raise StoreError("Codex terminal turn has pending requests or invalid status")
            success = turn["status"] == "completed"
            error = turn.get("error")
            if success and error is not None:
                raise StoreError("Codex completed status conflicts with a terminal error")
            for key in list(self.requests):
                self._withdraw(key, allow_partial=True)
            self.terminal = True
            events = [] if success else [("provider-status", error_status(error, interrupted=turn["status"] == "interrupted"))]
            events.append(("completed" if success else "failed", {"reason": turn["status"], "native_id": self.native_id}))
            return events
        self._scope(params)
        if method == "item/agentMessage/delta":
            delta = params.get("delta")
            if not isinstance(delta, str):
                raise StoreError("invalid Codex text delta")
            return [("text", {"text": delta[start:start + 16000]}) for start in range(0, len(delta), 16000)]
        if method in {"item/started", "item/completed"}:
            item = params.get("item")
            if not isinstance(item, dict):
                raise StoreError("invalid Codex item")
            iid = _text(item.get("id"), "item ID")
            kind = _text(item.get("type"), "item type")
            if kind == "collabAgentToolCall":
                raise StoreError("native Codex agent delegation is unsupported; use tracked Asha workers")
            if kind == "fileChange":
                size = len(json.dumps(item, ensure_ascii=True).encode())
                self.items.pop(iid, None)
                self.item_bytes -= self.item_sizes.pop(iid, 0)
                if size <= MAX_REVIEW_BYTES:
                    while self.items and (len(self.items) >= 128 or self.item_bytes + size > MAX_ITEM_CACHE_BYTES):
                        old_id = next(iter(self.items))
                        self.items.pop(old_id)
                        self.item_bytes -= self.item_sizes.pop(old_id)
                    self.items[iid] = item
                    self.item_sizes[iid] = size
                    self.item_bytes += size
                # Large ordinary patches still produce tool events. An exact
                # approval requiring omitted/evicted bytes remains unavailable.
            if kind in {"commandExecution", "fileChange", "mcpToolCall"}:
                detail = {"tool_id": iid, "name": kind}
                if item.get("status") is not None:
                    detail["status"] = _text(item["status"], "tool status", 128)
                if kind == "commandExecution" and method == "item/completed":
                    code, output = item.get("exitCode"), item.get("aggregatedOutput")
                    if code is not None:
                        if type(code) is int and -2147483648 <= code <= 2147483647:
                            detail["exit_code"] = code
                        else:
                            detail["exit_code_unavailable"] = True
                    if output is not None:
                        # Terminal item output may arrive without outputDelta.
                        # Retain the failure diagnosis in the bounded display
                        # store; tool failure does not rewrite the native turn.
                        if isinstance(output, str):
                            detail.update(output=output[-16000:], output_truncated=len(output) > 16000,
                                          output_section="tail" if len(output) > 16000 else "complete")
                        else:
                            detail["output_unavailable"] = True
                return [("tool", detail)]
            if kind == "userMessage" and item.get("clientId") == self.message_id:
                return [("progress", {"subtype": "native-input-acknowledged", "message_id": self.message_id})]
            return [("progress", {"subtype": kind})]
        if method == "error":
            if type(params.get("willRetry")) is not bool:
                raise StoreError("invalid Codex retry notification")
            events = [("progress", {"subtype": "provider-retrying" if params["willRetry"] else "provider-error"})]
            if not params["willRetry"]:
                events.append(("provider-status", {**error_status(params.get("error")), "source": "turn_error"}))
            return events
        if method.startswith(("item/", "turn/")) and "request" not in method.lower():
            return [("progress", {"subtype": method[:200]})]
        raise StoreError("unsupported Codex notification: " + method[:100])
