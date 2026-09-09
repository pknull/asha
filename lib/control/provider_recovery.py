"""Typed provider observations and durable, explicit recovery conditions.

Claude wire fields follow anthropics/claude-agent-sdk-python at
6bbd3093147c2fadcd4b868599b8fb6d9db3d523: types.py and
_internal/message_parser.py (rate_limit_event, assistant and result branches).
Display text and stderr never establish a quota or authorization condition.
"""
import json
import math
import time

from .record_registry import RecordRegistry
from .store import StoreError


REASONS = {"rate_limit", "authentication_failed", "billing_error", "invalid_request",
           "server_error", "unknown", "native_budget", "cancelled"}
SOURCES = {
    "claude": {"rate_limit_event", "assistant_error", "result_status", "result_reason"},
    "codex": {"turn_status", "turn_error"},
}


def _raw(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def validate_status(value):
    fields = {"contract", "provider", "source", "status", "reason", "reset_at", "window"}
    if (not isinstance(value, dict) or set(value) != fields
            or any(not isinstance(value[key], str) for key in ("contract", "provider", "source", "status", "reason"))
            or value["contract"] != "asha.provider-status.v1"
            or value["source"] not in SOURCES.get(value["provider"], set())
            or value["status"] not in {"allowed", "allowed_warning", "rejected"}
            or value["reason"] not in REASONS):
        raise StoreError("invalid provider status observation")
    reset = value["reset_at"]
    if reset is not None and (type(reset) not in {int, float} or not math.isfinite(reset) or not 0 <= reset <= 253402300799):
        raise StoreError("invalid provider rate-limit reset time")
    window = value["window"]
    if window is not None and (not isinstance(window, str) or not window or len(window) > 100
                               or not all(c.isascii() and (c.isalnum() or c in "_-") for c in window)):
        raise StoreError("invalid provider rate-limit window")
    return value


def _status(source, reason, *, status="rejected", reset=None, window=None):
    return validate_status({"contract": "asha.provider-status.v1", "provider": "claude",
        "source": source, "status": status, "reason": reason, "reset_at": reset, "window": window})


def claude_status(frame):
    kind = frame.get("type")
    if kind == "rate_limit_event":
        info = frame.get("rate_limit_info")
        if not isinstance(info, dict):
            raise StoreError("invalid provider rate-limit event")
        return _status("rate_limit_event", "rate_limit", status=info.get("status"),
                       reset=info.get("resetsAt"), window=info.get("rateLimitType"))
    if kind == "assistant" and frame.get("error") is not None:
        reason = frame["error"]
        if not isinstance(reason, str) or reason not in REASONS - {"native_budget", "cancelled"}:
            raise StoreError("unsupported structured provider assistant error")
        return _status("assistant_error", reason)
    if kind == "result":
        if frame.get("terminal_reason") in {"aborted_streaming", "aborted_tools"}:
            return _status("result_reason", "cancelled")
        if frame.get("subtype") in {"error_max_turns", "error_max_budget_usd"}:
            return _status("result_reason", "native_budget")
        status = frame.get("api_error_status")
        if status is not None:
            if type(status) is not int or not 100 <= status <= 599:
                raise StoreError("invalid provider API error status")
            if status >= 400 or frame.get("is_error"):
                reason = {429: "rate_limit", 401: "authentication_failed", 402: "billing_error"}.get(
                    status, "server_error" if status >= 500 else "invalid_request")
                return _status("result_status", reason)
    return None


def observe_status(c, sid, turn, payload):
    payload = validate_status(payload)
    registry = RecordRegistry("session-provider-status", scope=sid)
    old = registry.read(c, turn)
    previous = old["value"]["observation"] if old else None
    # Terminal assistant/result errors often omit metadata from an earlier
    # warning or rejection. A full event updates only its own limit window.
    if (old and payload["source"] != "rate_limit_event" and payload["reason"] == "rate_limit"
            and previous["reason"] == "rate_limit"):
        payload = {**payload, "reset_at": previous["reset_at"], "window": previous["window"]}
    after_terminal = RecordRegistry("session-terminal", scope=sid).read(c, turn) is not None
    observations = _observations(old)
    key = (payload["reason"], payload["window"])
    observations = [item for item in observations
                    if (item["observation"]["reason"], item["observation"]["window"]) != key]
    observations.append({"observation": payload, "after_terminal": after_terminal})
    if len(observations) > 128:
        raise StoreError("excessive provider status windows in one turn")
    registry.put(c, turn, _raw({"observation": payload, "after_terminal": after_terminal,
                               "observations": observations}),
                 expected_digest=old["digest"] if old else None, state=payload["status"])


def _observations(row):
    if row is None:
        return []
    value = row["value"]
    # Existing records predate per-window retention.
    return value.get("observations", [{"observation": value["observation"],
                                        "after_terminal": value["after_terminal"]}])


def observe_terminal(c, sid, turn, kind):
    RecordRegistry("session-terminal", scope=sid).put(c, turn, _raw({"kind": kind}), state=kind)


def recovery(c, sid):
    row = RecordRegistry("session-recovery").read(c, sid)
    if row is None or row["value"]["state"] != "pending":
        return None
    return row["value"]


def record_failure(c, sid, turn, generation, reason, *, input_not_submitted=False):
    observed = RecordRegistry("session-provider-status", scope=sid).read(c, turn)
    terminal = RecordRegistry("session-terminal", scope=sid).read(c, turn)
    rejected = [item["observation"] for item in _observations(observed)
                if item["observation"]["status"] == "rejected"]
    # Account/provider repairs remain actionable even while quota is exhausted.
    # Keep the time gate independent from the headline diagnosis.
    quota_conditions = [item for item in rejected if item["reason"] == "rate_limit"]
    repair_conditions = [item for item in rejected if item["reason"] != "rate_limit"]
    priority = ("authentication_failed", "billing_error", "native_budget", "cancelled",
                "invalid_request", "server_error", "unknown")
    condition = (min(repair_conditions, key=lambda item: priority.index(item["reason"])) if repair_conditions
                 else max(quota_conditions, key=lambda item: item["reset_at"] or 0) if quota_conditions else None)
    native_reason = condition["reason"] if condition else None
    category = {"rate_limit": "quota", "authentication_failed": "authentication", "billing_error": "billing",
                "native_budget": "native-budget", "cancelled": "cancelled"}.get(
                    native_reason, "provider" if terminal and terminal["value"]["kind"] == "failed" else "transport")
    resets = [item["reset_at"] for item in quota_conditions if item["reset_at"] is not None]
    reset = max(resets) if resets else None
    guidance = {
        "quota": "Confirm provider quota is available, inspect retained work, then resume explicitly",
        "authentication": "Restore provider authentication, inspect retained work, then resume explicitly",
        "billing": "Resolve provider billing, inspect retained work, then resume explicitly",
        "native-budget": "Review the native provider budget and retained work before an explicit new turn",
        "cancelled": "Inspect retained work and explicitly authorize a new turn if still wanted",
        "provider": "Confirm provider availability, inspect retained work, then resume explicitly",
        "transport": "Inspect retained work and native history before an explicit new turn; submission may have occurred",
    }[category]
    if quota_conditions and category != "quota":
        guidance += "; also confirm quota availability and honor its recorded reset"
    if category == "transport" and terminal and terminal["value"]["kind"] == "completed":
        guidance = "The provider reported this turn completed; inspect retained results and the later transport failure before an explicit new turn"
    if input_not_submitted:
        if category == "transport":
            guidance = "Restore provider transport, inspect retained work, then resume explicitly"
        guidance += "; input was never released to the provider; restate the retained assignment in the recovery input"
    elif not terminal:
        guidance += "; the original input will not replay; include any still-needed assignment in the recovery input"
    registry = RecordRegistry("session-recovery")
    old = registry.read(c, sid)
    value = {"contract": "asha.session-recovery.v1", "session_id": sid, "turn_id": turn,
             "generation": generation, "state": "pending", "category": category,
             "reason": str(reason or native_reason or "turn failed")[:1000].encode("utf-8", "backslashreplace").decode(), "retry_not_before": reset,
             "retry_condition": guidance, "provider_observation": condition,
             "provider_observations": rejected,
             "delivery": "terminal-" + ("failure" if terminal["value"]["kind"] == "failed" else "completed") if terminal else "not-submitted" if input_not_submitted else "uncertain",
             "recorded_at": time.time()}
    registry.put(c, sid, _raw(value), state="pending", expected_digest=old["digest"] if old else None)
    return value


def blocks_next_turn(c, sid, turn):
    row = RecordRegistry("session-provider-status", scope=sid).read(c, turn)
    return any(item["after_terminal"] and item["observation"]["status"] == "rejected"
               for item in _observations(row))


def resolve(c, sid, message_id, *, quota_reset_override=None):
    registry = RecordRegistry("session-recovery")
    old = registry.read(c, sid)
    if old is not None and old["value"]["state"] == "pending":
        value = {**old["value"], "state": "resolved", "resolution_message_id": message_id,
                 "quota_reset_override": quota_reset_override}
        registry.put(c, sid, _raw(value), state="resolved", expected_digest=old["digest"])


def recovery_receipt(c, sid, expected_digest, prompt_digest, max_turns, *, message_id=None, quota_reset_override=None):
    registry = RecordRegistry("session-recovery-commands", scope=sid)
    row = registry.read(c, expected_digest)
    if row is not None:
        if (row["value"]["prompt_digest"] != prompt_digest or row["value"]["max_turns"] != max_turns
                or row["value"].get("quota_reset_override") != quota_reset_override):
            raise StoreError("recovery already recorded with different content, budget or quota override")
        return row["value"]
    if message_id is not None:
        value = {"prompt_digest": prompt_digest, "max_turns": max_turns, "message_id": message_id,
                 "quota_reset_override": quota_reset_override}
        registry.put(c, expected_digest, _raw(value), state="recorded")
        return value
    return None
