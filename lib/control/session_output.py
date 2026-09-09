"""Bounded display output with immutable audit envelopes and explicit gaps."""
import hashlib
import json
from datetime import datetime, timezone

from .record_registry import RecordRegistry
from .store import StoreError


OUTPUT_KINDS = frozenset({"text", "progress", "tool"})
MAX_RECORDS = 256
MAX_BYTES = 1024 * 1024
MAX_CONSUMERS = 32
CONTRACT = "asha.session-output.v1"
MAX_CURSOR = 9223372036854775807


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def retention_record(c, sid):
    row = RecordRegistry("session-output-retention").read(c, sid)
    if row is not None:
        value = row["value"]
        if (set(value) != {"retired_through"} or type(value["retired_through"]) is not int
                or not 0 <= value["retired_through"] <= MAX_CURSOR):
            raise StoreError("invalid session output retention cursor")
    return row


def consumer_record(c, sid, consumer):
    row = RecordRegistry("session-event-consumers", scope=sid).read(c, consumer)
    if row is not None:
        value = row["value"]
        if (set(value) != {"session_id", "consumer", "through"} or value["session_id"] != sid
                or value["consumer"] != consumer or type(value["through"]) is not int
                or not 0 <= value["through"] <= MAX_CURSOR):
            raise StoreError("invalid session event consumer cursor")
    return row


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")


def _facts(kind, payload):
    fields = {"progress": ("subtype", "message_id", "pending_tasks"),
              "tool": ("tool_id", "name", "status", "exit_code")}.get(kind, ())
    return {key: payload[key] for key in fields if key in payload and (
        isinstance(payload[key], str) and len(payload[key].encode("utf-8", "backslashreplace")) <= 512
        or type(payload[key]) is int and (-2147483648 if key == "exit_code" else 0) <= payload[key] <= 2147483647)}


def envelope(payload, kind):
    raw = encode(payload)
    return {"output_ref": {"contract": CONTRACT, "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)},
            "facts": _facts(kind, payload)}


def retain(c, sid, sequence, payload):
    # session_events.sequence is AUTOINCREMENT; audit sequences are never reused.
    registry = RecordRegistry("session-output", scope=sid)
    # Keep escaped JSON inside the generic record: even diagnostic surrogate
    # bytes remain encodable in its text-search projection.
    registry.put(c, str(sequence).zfill(20), encode({"payload_json": encode(payload).decode("ascii")}), state="retained", updated_at=timestamp())
    rows = c.execute("SELECT record_key,length(CAST(payload AS BLOB)) AS bytes FROM records WHERE domain='session-output' AND scope=? ORDER BY record_key DESC", (sid,)).fetchall()
    total = 0
    retired = []
    for index, row in enumerate(rows):
        total += row["bytes"]
        if index >= MAX_RECORDS or total > MAX_BYTES:
            retired.append(row["record_key"])
    if retired:
        cursors = RecordRegistry("session-output-retention")
        old = retention_record(c, sid)
        through = max(int(key) for key in retired)
        if old:
            through = max(through, old["value"]["retired_through"])
        cursors.put(c, sid, encode({"retired_through": through}),
                    expected_digest=old["digest"] if old else None, state="bounded", updated_at=timestamp())
        # Only disposable display bodies retire. Audit events and their original
        # content hashes remain, as do messages, requests and approvals.
        for key in retired:
            c.execute("DELETE FROM records WHERE domain='session-output' AND scope=? AND record_key=?", (sid, key))


def project(c, event):
    """Resolve output or explain a proven retention gap; never fabricate text."""
    payload = json.loads(event["payload"])
    if not isinstance(payload, dict):
        event["payload"] = payload  # Preserve scalar/list legacy observations.
        return
    if event["kind"] not in OUTPUT_KINDS or set(payload) not in ({"output_ref"}, {"output_ref", "facts"}):
        event["payload"] = payload  # Pre-existing inline events remain readable.
        return
    ref = payload["output_ref"]
    facts = payload.get("facts", {})
    if not isinstance(facts, dict) or facts != _facts(event["kind"], facts):
        raise StoreError("invalid retained session output facts")
    if (not isinstance(ref, dict) or set(ref) != {"contract", "sha256", "bytes"}
            or ref["contract"] != CONTRACT or not isinstance(ref["sha256"], str)
            or len(ref["sha256"]) != 64 or any(char not in "0123456789abcdef" for char in ref["sha256"])
            or type(ref["bytes"]) is not int or ref["bytes"] < 0):
        raise StoreError("invalid session output reference")
    row = RecordRegistry("session-output", scope=event["session_id"]).read(c, str(event["sequence"]).zfill(20))
    if row is None:
        retention = retention_record(c, event["session_id"])
        if retention is None or event["sequence"] > retention["value"]["retired_through"]:
            raise StoreError("retained session output is missing without a retention record")
        event["payload"] = {**facts, "output_missing": True, "reason": "retention"}
        event["output"] = {**ref, "available": False, "reason": "retention"}
        return
    value = row["value"]
    if set(value) != {"payload_json"} or not isinstance(value["payload_json"], str):
        raise StoreError("invalid retained session output")
    try:
        raw = value["payload_json"].encode("ascii")
    except UnicodeError as exc:
        raise StoreError("invalid retained session output encoding") from exc
    if len(raw) != ref["bytes"] or hashlib.sha256(raw).hexdigest() != ref["sha256"]:
        raise StoreError("retained session output differs from its audit digest")
    event["payload"] = json.loads(raw)
    if "facts" in payload and facts != _facts(event["kind"], event["payload"]):
        raise StoreError("session output facts differ from the retained payload")
    event["output"] = {**ref, "available": True}
