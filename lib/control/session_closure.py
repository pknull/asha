"""Best-effort close of hub project sessions (docs/session-hub.md).

A close asks the session's own agent to save project Memory, waits a bounded
time, then terminates, and the row reads "closed, saved HH:MM" or "closed,
unsaved" from the generation's publication rows. Nothing here commits, pushes
or integrates code, and nothing here parses a transcript; the agent drafts,
Control validates.

Closure record (``row['closure']``)::

    request_id     UUID of this close request
    generation     hub incarnation the request was issued to
    state          closing | closed
    requested_at   when the request was recorded
    deadline       requested_at plus the wait; the close terminates then
    wait, forced   the wait in seconds; forced is a zero wait
    delivery       {channel, detail, message_id, delivered_at}
    pointer_at     when the one pointer line was typed (terminal only), or None
    memory         {available, destination, reason, baseline}
    handoff        None or the close-path handoff's evidence
    saved_at       the generation's latest publication when it closed, or None
    terminated_at

Records from earlier versions (states such as ``acknowledged`` or
``closed-no-save-claimed``, attempts, attention) are read-only: they present
through the same saved/unsaved label and never gate anything.
"""
from __future__ import annotations

import hashlib
import os
import stat
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .store import StoreError

_ROOT = Path(__file__).resolve().parents[2]
_SESSION_TOOLS = _ROOT / "plugins" / "session" / "tools"
if str(_SESSION_TOOLS) not in sys.path:
    sys.path.insert(0, str(_SESSION_TOOLS))

import memory_v2  # type: ignore  # noqa: E402
from path_safety import secure_path, secure_project_root  # type: ignore  # noqa: E402

MEMORY_FILES = ("activeContext.md", "decisions.md")
OUTCOMES = ("published", "no-durable-update", "failed", "blocked")
ACKNOWLEDGED = {"published", "no-durable-update"}
# Claude's Stop hook return channel is the only one proven to carry a block
# decision back to the model (docs/harness-enforcement.md).
STOP_HOOK_HARNESSES = {"claude"}
MESSAGE_KEY_PREFIX = "close:"
# The Stop bridge (control-event.sh) passes a block decision through only when
# its reason carries this token; keep the two in step.
CLOSE_REQUEST_TOKEN = "Asha Control close request"
# A publication still in flight at the deadline gets this long before the kill (D9).
PUBLICATION_GRACE_SECONDS = 10
# ``tmux.send_line`` refuses longer lines.
POINTER_LIMIT = 200
# The five-minute staleness rule: a working observation with nothing newer this long reads unknown.
STALE_OBSERVATION_SECONDS = 300


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _digest_or_none(path: Path):
    try:
        raw = _bounded_read(path, memory_v2.DECISIONS_LIMIT, path.name)[0]
    except FileNotFoundError:
        return None
    return _digest(raw)


def _bounded_read(path: Path, maximum: int, label: str) -> tuple[bytes, int]:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > maximum:
            raise ValueError(f"{label} is not one bounded regular file")
        if metadata.st_uid != os.geteuid():
            raise ValueError(f"{label} is not owned by the effective user")
        chunks, remaining = [], maximum + 1
        while remaining:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > maximum:
            raise ValueError(f"{label} exceeds {maximum} UTF-8 bytes")
        return raw, stat.S_IMODE(metadata.st_mode)
    finally:
        os.close(fd)


def memory_destination(project: str) -> dict:
    """Describe where this session's handoff would land, without writing."""
    try:
        root = secure_project_root(Path(project))
        memory_v2.require_v2_config(root)
    except (OSError, ValueError) as exc:
        return {"available": False, "destination": None, "baseline": None,
                "reason": f"Memory v2 is unavailable at {project}: {exc}", "saved": False}
    try:
        silenced = secure_path(root, "Work/markers/silence").exists()
        baseline = {name: _digest_or_none(secure_path(root, "Memory/" + name)) for name in MEMORY_FILES}
    except (OSError, ValueError) as exc:
        return {"available": False, "destination": str(root / "Memory"), "baseline": None,
                "reason": f"Memory v2 state is unreadable: {exc}", "saved": False}
    return {"available": not silenced, "destination": str(root / "Memory"), "baseline": baseline,
            "reason": "Memory persistence is disabled by Work/markers/silence" if silenced else None,
            "saved": False}


def new_closure(row: dict, *, wait: int) -> dict:
    now = time.time()
    return {"request_id": str(uuid.uuid4()), "generation": row["generation"], "state": "closing",
            "requested_at": now, "deadline": now + wait, "wait": wait, "forced": wait == 0,
            "delivery": {"channel": None, "detail": None, "message_id": None, "delivered_at": None},
            "pointer_at": None, "memory": memory_destination(row["project"]), "handoff": None,
            "saved_at": None, "terminated_at": None, "guidance": None}


def pending(record, row: dict) -> bool:
    """A current best-effort close request: this incarnation, with a deadline, not yet closed."""
    return (bool(record) and record.get("generation") == row["generation"] and record.get("state") == "closing"
            and record.get("deadline") is not None)


def message_key(record: dict) -> str:
    return MESSAGE_KEY_PREFIX + record["request_id"]


def request_text(row: dict, record: dict) -> str:
    """The close request. Delivered verbatim; contains no secrets."""
    rid, memory = record["request_id"], record["memory"]
    lines = [f"{CLOSE_REQUEST_TOKEN} {rid} for session {row['session_id']} (generation {row['generation']}).",
             "This session closes shortly. Bring the current step to a safe boundary and save project Memory now. "
             "Do not start new work, and do not commit, push or integrate code as part of this save."]
    if memory["available"]:
        baseline = memory["baseline"] or {}
        lines.append(f"Destination: {memory['destination']} (current digests: activeContext "
                     f"{baseline.get('activeContext.md') or 'none'}, decisions {baseline.get('decisions.md') or 'none'}).")
        lines.append("1. Re-read both published files; `asha control session handoff --read --json` prints the live digests. "
                     "2. If this session produced durable project knowledge, draft activeContext.md (exactly the level-one headings "
                     "Objective, State, Next, Blockers; at most 4096 bytes; at most five Next and five Blockers items) and "
                     "decisions.md (heading Decisions; current binding decisions only) outside Memory/ as absolute, symlink-free "
                     "paths you own, verify every claim against disk, then publish: `asha control session handoff --request " + rid +
                     " --active-file ACTIVE --decisions-file DECISIONS --expected-active DIGEST --expected-decisions DIGEST --json`. "
                     "If it reports a changed preimage, re-read, merge and retry. "
                     "3. If nothing durable changed: `asha control session handoff --request " + rid +
                     " --outcome no-durable-update --detail WHY --json`. "
                     "4. If publication is impossible: `--outcome blocked --detail REASON`.")
    else:
        lines.append(f"Project memory is unavailable ({memory['reason']}). Report the blocker with "
                     f"`asha control session handoff --request {rid} --outcome blocked --detail REASON --json`.")
    capture = record.get('capture', {})
    if capture.get('requested'):
        lines.append("Include one bounded JSON assessment (asha.session-experience.v1, at most 16 KiB, "
                     "three observations/four evidence items) with --experience-file FILE. Assessment is "
                     "observations, none-observed, or insufficient-evidence. Report facts, hypotheses and "
                     "uncertainty separately. Capture is independent of no-durable-update and never delays close. "
                     "Use --experience-ref REPORT_ID for unchanged findings; corrections use --supersedes REPORT_ID --key NEW_UUID.")
        if row.get('capture', {}).get('report_id'):
            lines.append('Previously captured report receipt: ' + row['capture']['report_id'])
    lines.append("Then end your turn. The session is terminated when the save lands or its wait runs out.")
    return "\n".join(lines)


def pointer_line(record: dict) -> str:
    """The one line typed into an idle or unobserved pane (D1): where the full request is."""
    line = (f"{CLOSE_REQUEST_TOKEN} {record['request_id']}: read it with `asha control session messages`, "
            "save project Memory as it says, then end your turn.")
    return line[:POINTER_LIMIT]


def _clock(stamp) -> str:
    return datetime.fromtimestamp(stamp, timezone.utc).strftime('%H:%M UTC')


def closed_label(row: dict) -> str:
    """``Closed, saved HH:MM UTC`` or ``Closed, unsaved`` (D8): unsaved only without any save in the generation."""
    word = "Stopped" if row.get("lifecycle") == "stopped" else "Closed"
    saved = row.get("memory_saved_at")
    return f"{word}, " + ("saved " + _clock(saved) if saved is not None else "unsaved")


def guidance_for(row: dict, record: dict) -> str:
    if row.get("lifecycle") == "closing":
        if record.get("deadline") is None:
            return "Close request from an earlier version; run close again to ask for a save and close"
        saved = row.get("memory_saved_at")
        if saved is not None and saved >= record.get("requested_at", float("inf")):
            return "Closing: saved " + _clock(saved)
        return "Closing: asked for a Memory save; closes by " + _clock(record["deadline"])
    return closed_label(row)


def receipt_for(record: dict) -> dict:
    """The identity a Stop delivery is confirmed against: request and incarnation."""
    return {"request_id": record["request_id"], "generation": record["generation"]}


class StopDecision(dict):
    """The harness-facing block decision plus the private receipt that confirms it.

    The dict itself is exactly ``{"decision": "block", "reason": ...}`` so the
    bridge's strict shape check still holds; the receipt never leaves Control.
    """

    def __init__(self, reason: str, *, receipt: dict):
        super().__init__(decision="block", reason=reason)
        self.receipt = receipt


def mark_delivered(record: dict, channel: str, *, detail: str, message_id=None) -> dict:
    return dict(record, delivery={"channel": channel, "detail": detail, "message_id": message_id,
                                  "delivered_at": time.time()})


def validate_handoff_request(record, row, request_id: str):
    if not pending(record, row):
        raise StoreError("no close request is pending for this session")
    if record["request_id"] != request_id:
        raise StoreError("stale close request; the current request or session incarnation differs")


def record_handoff(record: dict, row: dict, outcome: str, detail: str, *, publication=None) -> dict:
    """Keep the close-path handoff as evidence on the record; the close itself reads publication rows."""
    if outcome not in OUTCOMES:
        raise StoreError("invalid handoff outcome")
    handoff = {"outcome": outcome, "detail": detail, "acknowledged_at": time.time(),
               "generation": row["generation"], "destination": record["memory"].get("destination"),
               "digests": None, "changed": []}
    if outcome == "published":
        if not publication:
            raise StoreError("published outcome requires a verified publication")
        handoff.update(digests=publication["digests"], changed=publication["changed"],
                       destination=publication["destination"], current=publication.get("current"),
                       superseded=publication.get("superseded"), verified=publication.get("verified", True),
                       verification_error=publication.get("verification_error"))
    return dict(record, handoff=handoff)


def publish_handoff(project: str, active_file: str, decisions_file: str, *, expected: dict) -> dict:
    """Publish through the shared validator with a compare-and-swap on both files.

    ``expected`` maps ``activeContext.md``/``decisions.md`` to the digests the
    agent read before drafting; a differing live digest refuses the write so a
    newer publication is never overwritten. No Git seam exists on this path.
    """
    root = secure_project_root(Path(project))
    active_raw, _ = _bounded_read(_draft_path(active_file, "active draft"), memory_v2.ACTIVE_LIMIT, "active draft")
    decisions_raw, _ = _bounded_read(_draft_path(decisions_file, "decisions draft"), memory_v2.DECISIONS_LIMIT, "decisions draft")
    try:
        active, decisions = active_raw.decode("utf-8"), decisions_raw.decode("utf-8")
    except UnicodeError as exc:
        raise ValueError("handoff drafts must be UTF-8") from exc
    preimages = {"active": expected.get("activeContext.md"), "decisions": expected.get("decisions.md")}
    try:
        receipt = memory_v2.publish(root, active, decisions, expected_preimages=preimages, publication_source="close")
    except ValueError as exc:
        if "preimage changed" in str(exc):
            raise ValueError("publication preimage changed: the live Memory digests differ from --expected-active/"
                             "--expected-decisions (omitted, stale, or another save landed first); run "
                             "`asha control session handoff --read --json`, merge, and retry") from exc
        raise
    # publish() returns only after both atomic replacements committed under the
    # project lock, so the drafts are on disk. A later publisher may already have
    # replaced them; that is reported as superseded, never as a failed save.
    digests = {"activeContext.md": _digest(active_raw), "decisions.md": _digest(decisions_raw)}
    changed = [name for name in MEMORY_FILES if digests[name] != expected.get(name)]
    result = {"destination": str(root / "Memory"), "digests": digests, "changed": changed,
              "publication": receipt,
              "current": None, "superseded": None, "verified": False, "verification_error": None,
              "git_invoked": False}
    try:
        # Best effort: another publisher's in-flight journal makes this read refuse,
        # which says nothing about our committed write.
        after = memory_v2.read_published_snapshot(root)
    except (OSError, ValueError) as exc:
        result["verification_error"] = str(exc)[:500]
        return result
    current = {"activeContext.md": _digest(after.active_context), "decisions.md": _digest(after.decisions)}
    result.update(current=current, superseded=current != digests, verified=True)
    return result


def _draft_path(value, label: str) -> Path:
    """Drafts are the agent's own files: absolute, symlink-free, readable by this user."""
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} path is required")
    absolute = Path(os.path.abspath(value))
    try:
        if absolute.resolve(strict=True) != absolute:
            raise ValueError(f"{label} path contains a symlink: {absolute}")
    except OSError as exc:
        raise ValueError(f"{label} is unreadable: {exc}") from exc
    return absolute


def parse_digest(value):
    if value is None or value == "none":
        return None
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise StoreError("expected digest must be a lowercase sha256 hex string or 'none'")
    return value


__all__ = ["ACKNOWLEDGED", "CLOSE_REQUEST_TOKEN", "MEMORY_FILES", "MESSAGE_KEY_PREFIX", "OUTCOMES",
           "PUBLICATION_GRACE_SECONDS", "STOP_HOOK_HARNESSES", "StopDecision", "closed_label", "guidance_for",
           "mark_delivered", "memory_destination", "message_key", "new_closure", "parse_digest", "pending",
           "pointer_line", "publish_handoff", "receipt_for", "record_handoff", "request_text",
           "validate_handoff_request"]
