"""Bounded Claude SDK control frames, independent of processes and storage.

The installed 2.1.263 initialize/stdio handshake is verified separately. Native
permission operations are delegated to the trusted owner's durable methods.
"""
from __future__ import annotations

from collections import deque
import json
import uuid

from .store import StoreError


class ClaudeProtocol:
    def __init__(self, prompt, *, native_id=None, message_id=None,
                 open_request=None, cancel_request=None, poll_responses=None, submitted=None):
        self.initialize_id = "asha-init-" + str(uuid.uuid4())
        self.prompt, self.native_id = prompt, native_id
        self.message_id = message_id or str(uuid.uuid4())
        self.open_request, self.cancel_request = open_request, cancel_request
        self.poll_responses, self.submitted = poll_responses, submitted
        self.initialized = False
        self.terminal = False
        self.requests = {}
        self.seen = {}
        self.tasks = set()
        self.outbound = deque()
        self.bytes_queued = 0
        self.queue({"type": "control_request", "request_id": self.initialize_id,
                    "request": {"subtype": "initialize", "hooks": None}})

    def queue(self, frame, local_id=None):
        raw = (json.dumps(frame, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n").encode()
        if self.bytes_queued + len(raw) > 1024 * 1024:
            raise StoreError("native control output queue overflow")
        provider_id = frame.get("response", {}).get("request_id")
        self.outbound.append({"raw": raw, "offset": 0, "local_id": local_id, "provider_id": provider_id})
        self.bytes_queued += len(raw)

    def chunk(self):
        if not self.outbound:
            return b""
        item = self.outbound[0]
        return item["raw"][item["offset"]:item["offset"] + 4096]

    def advance(self, count):
        item = self.outbound[0]
        item["offset"] += count
        self.bytes_queued -= count
        if item["offset"] == len(item["raw"]):
            self.outbound.popleft()
            if item["local_id"] is not None:
                self.submitted(item["local_id"])
                self.requests.pop(item["provider_id"], None)

    def poll(self):
        if not self.requests or self.poll_responses is None or self.terminal:
            return
        for item in self.poll_responses():
            provider_id = item["frame"]["response"]["request_id"]
            if provider_id not in self.requests:
                raise StoreError("native reply does not match a pending control request")
            self.queue(item["frame"], item["request_id"])

    def feed(self, value):
        from .session_harness import decode_claude
        if not isinstance(value, dict):
            raise StoreError("native protocol frame must be an object")
        kind = value.get("type")
        if self.terminal:
            if kind == "rate_limit_event":
                return list(decode_claude(value))
            # Claude 2.1.263 emits command bookkeeping after the result. This
            # carries no model work and cannot change the retained outcome.
            if kind == "command_lifecycle" or (kind == "system" and value.get("subtype") in
                    ("session_state_changed", "hook_started", "hook_progress", "hook_response")):
                return []
            raise StoreError("provider emitted work after its terminal result")
        if kind == "control_response":
            response = value.get("response")
            if (not isinstance(response, dict) or response.get("request_id") != self.initialize_id
                    or response.get("subtype") != "success" or self.initialized):
                raise StoreError("native initialization failed or returned an unexpected response")
            self.initialized = True
            self.queue({"type": "user", "session_id": self.native_id or "", "uuid": self.message_id,
                        "parent_tool_use_id": None, "message": {"role": "user", "content": self.prompt}})
            return []
        if not self.initialized:
            # Asha's SessionStart hooks run before the SDK handshake completes.
            # Their status is observable, but cannot release queued model input.
            if kind == "system" and value.get("subtype") in ("hook_started", "hook_progress", "hook_response"):
                return list(decode_claude(value))
            if kind == "rate_limit_event":
                return list(decode_claude(value))
            raise StoreError("native provider emitted work before initialization acknowledgment")
        if kind == "control_request":
            request_id, payload = value.get("request_id"), value.get("request")
            if not isinstance(request_id, str) or not request_id or len(request_id) > 512 or not isinstance(payload, dict):
                raise StoreError("malformed native control request")
            if payload.get("subtype") != "can_use_tool" or self.open_request is None:
                raise StoreError("unsupported native control request")
            try:
                canonical = json.dumps(payload, sort_keys=True, allow_nan=False)
            except (ValueError, TypeError, RecursionError) as exc:
                raise StoreError("malformed native permission JSON") from exc
            if request_id in self.seen:
                if self.seen[request_id] != canonical or request_id not in self.requests:
                    raise StoreError("native request ID was reused or reissued after resolution")
                return []
            if len(self.seen) >= 128:
                raise StoreError("native permission request limit exceeded")
            self.open_request(request_id, payload)
            self.seen[request_id] = canonical
            self.requests[request_id] = payload
            return []
        if kind == "control_cancel_request":
            request_id = value.get("request_id")
            if not isinstance(request_id, str) or request_id not in self.seen or self.cancel_request is None:
                raise StoreError("native cancellation references an unknown request")
            self.cancel_request(request_id)
            self.requests.pop(request_id, None)
            retained = deque()
            for item in self.outbound:
                if item["provider_id"] == request_id and not item["offset"]:
                    self.bytes_queued -= len(item["raw"])
                else:
                    retained.append(item)
            self.outbound = retained
            return []
        if kind == "system":
            task_id = value.get("task_id")
            subtype = value.get("subtype")
            if subtype in ("task_started", "task_notification"):
                if not isinstance(task_id, str) or not task_id or len(task_id) > 512:
                    raise StoreError("invalid native background task ID")
                field = "task_type" if subtype == "task_started" else "status"
                if not isinstance(value.get(field), str):
                    raise StoreError("invalid native background task metadata")
                if subtype == "task_started" and value[field] in {"local_agent", "local_workflow"}:
                    if len(self.tasks) >= 128 and task_id not in self.tasks:
                        raise StoreError("excessive native background tasks")
                    self.tasks.add(task_id)
                elif subtype == "task_notification" and value[field] in {"completed", "failed", "stopped"}:
                    self.tasks.discard(task_id)
        if kind == "result":
            if self.requests:
                raise StoreError("native turn ended with unanswered permission requests")
            if self.tasks:
                return [("progress", {"subtype": "intermediate-result", "pending_tasks": len(self.tasks)})]
            self.terminal = True
        # Replayed user input is provider custody, not evidence of model consumption.
        if kind == "user" and value.get("uuid") == self.message_id:
            return [("progress", {"subtype": "native-input-acknowledged", "message_id": self.message_id})]
        return list(decode_claude(value))
