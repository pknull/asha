"""Graceful closure of hub project sessions with a verified memory handoff.

A normal close asks the session's own agent for one final turn: reach a safe
boundary, publish durable project knowledge through the existing Memory v2
validator, and acknowledge. Only a verified acknowledgement lets the close
terminate the process. Nothing here commits, pushes or integrates code, and
nothing here parses a transcript; the agent drafts, Control validates.

Closure record (``row['closure']``)::

    request_id     UUID of this close request
    generation     hub incarnation the request was issued to
    state          pending-delivery | delivered | acknowledged | completed |
                   unanswered | handoff-failed | undeliverable | unavailable |
                   forced | closed-no-save-claimed
    delivery       {channel, detail, message_id, delivered_at}
    memory         {available, destination, reason, baseline}
    handoff        None or {outcome, detail, acknowledged_at, generation,
                   destination, digests, changed}
    attempts, requested_at, terminated_at, last_error, guidance

``memory.saved`` is true only after a verified ``published`` outcome.
"""
from __future__ import annotations

import hashlib
import os
import stat
import sys
import time
import uuid
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
# decision back to the model (docs/harness-enforcement.md). Codex delivery is
# live-proven but its return channel is not claimed; a live probe may add it.
STOP_HOOK_HARNESSES = {"claude"}
# Issue #101: the operator closed an idle session without asking for a turn.
# Distinct from "completed" (a verified receipt) and "forced" (no boundary).
NO_HANDOFF_STATE = "closed-no-save-claimed"
TERMINAL_STATES = {"completed", "forced", "unavailable", "undeliverable", NO_HANDOFF_STATE}
RETRYABLE_STATES = {"unanswered", "handoff-failed"}
# Terminated without a verified save through no explicit operator choice: kept
# on the default page until the operator acknowledges it.
ATTENTION_STATES = {"unanswered", "handoff-failed", "undeliverable", "unavailable"}
MESSAGE_KEY_PREFIX = "close:"
# Issue #96: the close request typed into an owned, detached, idle terminal pane
# whose input line was proven empty (Claude and Codex only).
INJECTION_CHANNEL = "pane-injection"
# The Stop bridge (control-event.sh) passes a block decision through only when
# its reason carries this token; keep the two in step.
CLOSE_REQUEST_TOKEN = "Asha Control close request"


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


def new_closure(row: dict) -> dict:
    memory = memory_destination(row["project"])
    return {"request_id": str(uuid.uuid4()), "generation": row["generation"], "state": "pending-delivery",
            "requested_at": time.time(), "attempts": 1, "delivery": {"channel": None, "detail": None,
            "message_id": None, "delivered_at": None}, "memory": memory, "handoff": None,
            "terminated_at": None, "last_error": None, "guidance": None}


def message_key(closure: dict) -> str:
    """One retained message per delivery attempt; a re-armed request gets a fresh queued copy."""
    attempts = closure.get("attempts", 1)
    return MESSAGE_KEY_PREFIX + closure["request_id"] + ("" if attempts <= 1 else f":{attempts}")


def request_text(row: dict, closure: dict) -> str:
    """The final-turn instruction. Delivered verbatim; contains no secrets."""
    rid, memory = closure["request_id"], closure["memory"]
    selector = rid + ' --attempt ' + str(closure.get('attempts', 1))
    lines = [f"{CLOSE_REQUEST_TOKEN} {rid} for session {row['session_id']} (generation {row['generation']}).",
             "Bring the current step to a safe boundary, then leave a verified project-memory handoff before this session ends. "
             "Do not start new work, and do not commit, push or integrate code as part of this handoff."]
    if memory["available"]:
        baseline = memory["baseline"] or {}
        lines.append(f"Destination: {memory['destination']} (current digests: activeContext "
                     f"{baseline.get('activeContext.md') or 'none'}, decisions {baseline.get('decisions.md') or 'none'}).")
        lines.append("Run the handoff command as the only command in its own tool call, after every other tool has finished, "
                     "and do not chain it with other commands.")
        lines.append("1. Re-read both published files; `asha control session handoff --read --json` prints the live digests. "
                     "2. If this session produced durable project knowledge, draft activeContext.md (exactly the level-one headings "
                     "Objective, State, Next, Blockers; at most 4096 bytes; at most five Next and five Blockers items) and "
                     "decisions.md (heading Decisions; current binding decisions only) outside Memory/ as absolute, symlink-free "
                     "paths you own, verify every claim against disk, then publish: `asha control session handoff --request " + selector +
                     " --active-file ACTIVE --decisions-file DECISIONS --expected-active DIGEST --expected-decisions DIGEST --json`. "
                     "If it reports a changed preimage, re-read, merge and retry. "
                     "3. If nothing durable changed: `asha control session handoff --request " + selector + " --outcome no-durable-update --detail WHY --json`. "
                     "4. If publication is impossible: `--outcome blocked --detail REASON`.")
    else:
        lines.append(f"Project memory is unavailable ({memory['reason']}). Report the blocker with "
                     f"`asha control session handoff --request {selector} --outcome blocked --detail REASON --json`. "
                     "Unavailable memory cannot authorize a no-durable-update completion.")
    capture = closure.get('capture', {})
    if capture.get('requested'):
        lines.append("Include one bounded JSON assessment (asha.session-experience.v1, at most 16 KiB, "
                     "three observations/four evidence items) with --experience-file FILE. Assessment is "
                     "observations, none-observed, or insufficient-evidence. Report facts, hypotheses and "
                     "uncertainty separately. Capture is independent of no-durable-update and never delays close. "
                     "Use --experience-ref REPORT_ID for unchanged findings; corrections use --supersedes REPORT_ID --key NEW_UUID.")
        if row.get('capture', {}).get('report_id'):
            lines.append('Previously captured report receipt: ' + row['capture']['report_id'])
    lines.append("After the handoff is acknowledged, end your turn. The operator terminates the session only after that acknowledgement.")
    return "\n".join(lines)


def recent_native_observation(row: dict) -> bool:
    stamp = row.get('native_observed_at', row.get('observed_at'))
    if stamp is not None and 0 <= time.time() - stamp <= 300:
        return True
    record = row.get('closure') or {}
    return record.get('generation') == row.get('generation') and recent_injection(record)


def recent_injection(record: dict) -> bool:
    """A request typed into a verified idle pane is fresh delivery evidence (#96)."""
    delivery = (record or {}).get('delivery') or {}
    at = delivery.get('delivered_at')
    return delivery.get('channel') == INJECTION_CHANNEL and at is not None and 0 <= time.time() - at <= 300


def guidance_for(row: dict, closure: dict, *, idle_delivery: bool = False) -> str:
    state = closure["state"]
    if closure.get('attachment_required') and state not in {'completed', 'forced', 'handoff-failed', NO_HANDOFF_STATE}:
        return 'Close needs attachment: attach and hand the agent the retained close request, or force-close. ' + str(closure.get('last_error') or '')
    if state == "pending-delivery":
        if row["transport"] == "structured":
            return "Close request queued as the next structured turn; re-run close after it completes"
        if row["harness"] in STOP_HOOK_HARNESSES:
            typed = "or typed into its pane at a verified idle boundary; " if idle_delivery else ""
            return ("Close request queued; it reaches the agent when its current turn stops, when it reads messages, "
                    + typed + "otherwise attach and hand it the request, or force-close")
        return ("Close request queued; this harness has no proven Stop return channel, so it reaches the agent only when "
                "it reads messages: attach and hand it the request, or force-close")
    if state == "delivered":
        if (closure.get("delivery") or {}).get("channel") == INJECTION_CHANNEL:
            return "Close request typed into the idle session; waiting for its handoff acknowledgement"
        return "Close request delivered; waiting for the session's handoff acknowledgement"
    if state == "acknowledged":
        return "Handoff " + _verification_word(closure) + "; re-run close (or close --wait) to terminate the session"
    if state == "unanswered":
        return "The agent's turn ended without a handoff; re-run close to ask again, or force-close"
    if state == "handoff-failed":
        outcome = (closure.get("handoff") or {}).get("outcome", "failed")
        if outcome in ACKNOWLEDGED and closure.get("last_error"):
            # The agent answered correctly; Control refused its completion evidence.
            return (f"Handoff answered {outcome}, but Control refused the completion receipt: "
                    f"{closure['last_error']}. Re-run close to retry, or force-close")
        return f"Handoff reported {outcome}; project memory was not saved. Re-run close to retry, or force-close"
    if state == "undeliverable":
        return ("Close request could not be delivered (" + str(closure.get("last_error") or "the harness is no longer live")
                + "); project memory was not saved by this session")
    if state == "unavailable":
        return "No live agent could be asked for a handoff; project memory was not saved by this session"
    if state == "forced":
        handoff = closure.get("handoff") or {}
        if handoff.get("outcome") in ACKNOWLEDGED:
            return "Force-closed after a " + _verification_word(closure) + " handoff (" + handoff["outcome"] + ")"
        return "Force-closed; no project-memory handoff was claimed"
    if state == NO_HANDOFF_STATE:
        return ("Closed at a native idle boundary without a handoff turn (--no-handoff); "
                "no project-memory save was claimed by this close")
    if state == "completed":
        outcome = (closure.get("handoff") or {}).get("outcome")
        return "Closed after a " + _verification_word(closure) + " handoff (" + str(outcome) + ")"
    return state


def _verification_word(closure: dict) -> str:
    handoff = closure.get("handoff") or {}
    if handoff.get("outcome") == "published" and handoff.get("verified") is False:
        return "published but unverified (post-publish read refused: " + str(handoff.get("verification_error")) + ")"
    return "verified"


def receipt_for(closure: dict) -> dict:
    """The identity a Stop delivery is confirmed against: request, attempt and incarnation."""
    return {"request_id": closure["request_id"], "attempts": closure.get("attempts", 1),
            "generation": closure["generation"]}


class StopDecision(dict):
    """The harness-facing block decision plus the private receipt that confirms it.

    The dict itself is exactly ``{"decision": "block", "reason": ...}`` so the
    bridge's strict shape check still holds; the receipt never leaves Control.
    """

    def __init__(self, reason: str, *, receipt: dict):
        super().__init__(decision="block", reason=reason)
        self.receipt = receipt


def transition(closure: dict, state: str, **changes) -> dict:
    updated = dict(closure)
    updated.update(changes)
    updated["state"] = state
    return updated


def mark_delivered(closure: dict, channel: str, *, detail: str, message_id=None) -> dict:
    # Delivery answers an earlier "needs attach" for this attempt.
    return transition(closure, "delivered", delivery={"channel": channel, "detail": detail,
                      "message_id": message_id, "delivered_at": time.time()},
                      attachment_required=False, input_refusal=None)


def rearm(closure: dict) -> dict:
    previous = closure.get("handoff")
    return transition(closure, "pending-delivery", attempts=closure.get("attempts", 1) + 1, handoff=None,
                      previous_handoff=previous, delivery={"channel": None, "detail": None,
                      "message_id": None, "delivered_at": None})


def validate_handoff_request(closure, row, request_id: str):
    if not closure or closure.get("state") in TERMINAL_STATES:
        raise StoreError("no close request is pending for this session")
    if closure["request_id"] != request_id or closure["generation"] != row["generation"]:
        raise StoreError("stale close request; the current request or session incarnation differs")
    if closure.get("handoff"):
        raise StoreError("close request already acknowledged")


def record_handoff(closure: dict, row: dict, outcome: str, detail: str, *, publication=None) -> dict:
    if outcome not in OUTCOMES:
        raise StoreError("invalid handoff outcome")
    handoff = {"outcome": outcome, "detail": detail, "acknowledged_at": time.time(),
               "generation": row["generation"], "destination": closure["memory"].get("destination"),
               "digests": None, "changed": []}
    memory = dict(closure["memory"])
    if outcome == "published":
        if not publication:
            raise StoreError("published outcome requires a verified publication")
        handoff.update(digests=publication["digests"], changed=publication["changed"],
                       destination=publication["destination"], current=publication.get("current"),
                       superseded=publication.get("superseded"), verified=publication.get("verified", True),
                       verification_error=publication.get("verification_error"))
        memory["saved"] = True
    state = "acknowledged" if outcome in ACKNOWLEDGED else "handoff-failed"
    return transition(closure, state, handoff=handoff, memory=memory, last_error=None, attachment_required=False)


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


__all__ = ["ACKNOWLEDGED", "ATTENTION_STATES", "CLOSE_REQUEST_TOKEN", "INJECTION_CHANNEL", "MEMORY_FILES", "MESSAGE_KEY_PREFIX",
           "NO_HANDOFF_STATE",
           "OUTCOMES", "RETRYABLE_STATES", "STOP_HOOK_HARNESSES", "TERMINAL_STATES", "StopDecision",
           "guidance_for", "mark_delivered", "memory_destination", "message_key", "new_closure",
           "parse_digest", "publish_handoff", "rearm", "receipt_for", "recent_injection", "record_handoff", "request_text",
           "transition", "validate_handoff_request"]
