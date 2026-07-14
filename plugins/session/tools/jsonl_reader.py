#!/usr/bin/env python3
"""jsonl_reader — read native session transcripts and emit Asha events.

Replaces hook-driven event capture (post-tool-use.sh +
user-prompt-submit.sh appending to Memory/events/events.jsonl) with
on-demand parse-at-/save of the host's own session log.

The hosts already write a richer transcript than the hooks could
capture; this module just reads it and shapes events into the same dict
schema event_store.py emits, so pattern_analyzer.py and other consumers
need no changes.

Four harness branches:
  - claude:  ~/.claude/projects/<slug>/<sid>.jsonl
  - codex:   ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl
  - copilot: ~/.copilot/session-state/<sid>/events.jsonl
  - opencode: ~/.local/share/opencode/storage/{session,message,part}/...json

Three public functions:
  - locate_session_log(harness)  -> Path | None
  - stream_events(path, harness) -> Iterator[Event]
  - to_synth_events(events, project_dir, session_id) -> list[dict]
"""

from __future__ import annotations

import json
import os
import re
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator, Optional

# Shared streaming utilities (factored out of bin/asha-search-index.py).
sys.path.insert(0, os.path.expanduser("~/life/bin"))
try:
    from asha_jsonl import stream_jsonl  # type: ignore[import-not-found]
except ImportError:
    # Fallback minimal streamer — keeps the module importable even if
    # ~/life/bin/asha_jsonl.py is missing in the environment.
    def stream_jsonl(path: Path, label: str | None = None) -> Iterator[tuple[str, dict]]:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for lineno, raw in enumerate(fh, start=1):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    parsed = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, dict):
                    yield str(lineno), parsed


# ---------------------------------------------------------------------------
# Normalized event shape
# ---------------------------------------------------------------------------

@dataclass
class Event:
    """Harness-agnostic event yielded by stream_events.

    `kind` values consumed by to_synth_events:
      - "prompt"          : real user message
      - "assistant_text"  : assistant prose (currently dropped by synth)
      - "tool_use"        : assistant invoked a tool
      - "tool_result"     : tool returned (used for error detection)
      - "skill"           : skill / slash-command invocation
      - "agent"           : subagent spawn
      - "meta"            : unknown / unmapped line — preserves raw for audit
    """
    timestamp: str
    kind: str
    actor: str = ""           # "user" | "assistant" | ""
    tool: Optional[str] = None
    text: str = ""
    detail: str = ""
    raw: dict = field(default_factory=dict)


@dataclass(frozen=True)
class SessionIdentity:
    """Resolved save-run identity.

    The save pipeline must treat this triple as immutable: parser harness,
    session id, and the exact transcript path must agree before synthesis.
    """
    harness: str
    session_id: str
    transcript_path: Path


class IdentityError(RuntimeError):
    """Raised when a save run's harness/session/transcript identity is unsafe."""


# WWA provenance is consumed both before synthesis (drift classification) and
# after synthesis (save_preflight's hard gate). Keep the parser here beside the
# transcript identity primitives so the two stages cannot drift apart.
_WWA_HEADER_RE = re.compile(r"^##\s+What Was Accomplished\b.*$", re.MULTILINE)
_WWA_SESSION_RE = re.compile(r"<!--\s*wwa-session:\s*(\S+?)\s*-->")


def lead_wwa_body(text: str) -> Optional[str]:
    """Return the first WWA section body, or None when no WWA exists."""
    match = _WWA_HEADER_RE.search(text)
    if not match:
        return None
    start = match.end()
    next_section = re.search(r"^##\s", text[start:], re.MULTILINE)
    return text[start:start + next_section.start()] if next_section else text[start:]


def lead_wwa_session(text: str) -> Optional[str]:
    """Return the provenance stamp from the lead WWA section."""
    body = lead_wwa_body(text)
    if body is None:
        return None
    match = _WWA_SESSION_RE.search(body)
    return match.group(1) if match else None


HARNESS_CHOICES = {"claude", "codex", "copilot", "opencode"}
_CODEX_ROLLOUT_RE = re.compile(
    r"^rollout-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-"
    r"([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})\.jsonl$",
    re.IGNORECASE,
)


def _read_first_json(path: Path) -> Optional[dict]:
    if path.suffix == ".json":
        try:
            data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
            return data if isinstance(data, dict) else None
        except (OSError, json.JSONDecodeError):
            return None
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                data = json.loads(raw)
                return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None
    return None


def sniff_harness(path: Path) -> Optional[str]:
    """Infer transcript harness from the first JSON object shape."""
    data = _read_first_json(path)
    if not data:
        return None
    typ = str(data.get("type", ""))
    payload = data.get("payload") or {}
    if typ == "session_meta" and isinstance(payload, dict) and "id" in payload:
        return "codex"
    if typ.startswith("session.") or typ in {"user.message", "assistant.turn_start"}:
        return "copilot"
    if str(data.get("id", "")).startswith("ses_") and "directory" in data:
        return "opencode"
    if "sessionId" in data:
        return "claude"
    return None


def transcript_session_id(path: Path, harness: str) -> Optional[str]:
    """Extract the native session id embedded in a transcript, if available."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(data, dict):
                    continue
                if harness == "codex":
                    payload = data.get("payload") or {}
                    if data.get("type") == "session_meta" and isinstance(payload, dict):
                        sid = payload.get("id")
                        if sid:
                            return str(sid)
                elif harness == "claude":
                    sid = data.get("sessionId")
                    if sid:
                        return str(sid)
                elif harness == "copilot":
                    data_obj = data.get("data") or {}
                    if isinstance(data_obj, dict):
                        sid = data_obj.get("sessionId")
                        if sid:
                            return str(sid)
                elif harness == "opencode":
                    sid = data.get("id")
                    if str(sid).startswith("ses_"):
                        return str(sid)
    except OSError:
        return None
    if harness == "codex":
        m = _CODEX_ROLLOUT_RE.match(path.name)
        if m:
            return m.group(1)
    if harness == "claude" and path.suffix == ".jsonl":
        return path.stem
    if harness == "copilot" and path.name == "events.jsonl":
        return path.parent.name
    if harness == "opencode" and path.suffix == ".json" and path.stem.startswith("ses_"):
        return path.stem
    return None


def transcript_session_ids(path: Path, harness: str) -> set[str]:
    """Collect every explicit native session stamp present in a transcript."""
    found: set[str] = set()
    try:
        for _, data in stream_jsonl(path, label="session identity scan"):
            sid = None
            if harness == "claude":
                sid = data.get("sessionId")
            elif harness == "codex" and data.get("type") == "session_meta":
                payload = data.get("payload") or {}
                sid = payload.get("id") if isinstance(payload, dict) else None
            elif harness == "copilot":
                payload = data.get("data") or {}
                sid = payload.get("sessionId") if isinstance(payload, dict) else None
            elif harness == "opencode":
                candidate = data.get("id")
                sid = candidate if str(candidate).startswith("ses_") else None
            if sid:
                found.add(str(sid))
    except OSError:
        return set()
    return found


def transcript_path_session_id(path: Path, harness: str) -> Optional[str]:
    """Extract only the session-id component carried by the native path."""
    if harness == "claude" and path.suffix == ".jsonl":
        return path.stem
    if harness == "codex":
        match = _CODEX_ROLLOUT_RE.match(path.name)
        return match.group(1) if match else None
    if harness == "copilot" and path.name == "events.jsonl":
        return path.parent.name
    if harness == "opencode" and path.suffix == ".json" and path.stem.startswith("ses_"):
        return path.stem
    return None


def _native_session_id(harness: str) -> Optional[str]:
    if harness == "claude":
        return os.environ.get("CLAUDE_CODE_SESSION_ID")
    if harness == "codex":
        return os.environ.get("CODEX_THREAD_ID")
    if harness == "copilot":
        return os.environ.get("COPILOT_SESSION_ID")
    if harness == "opencode":
        return os.environ.get("OPENCODE_SESSION_ID")
    return None


def detect_harness_from_env() -> Optional[str]:
    """Detect active harness without guessing across nested harnesses.

    ASHA_HARNESS is authoritative. Without it, exactly one native harness marker
    may be present; multiple markers means a nested tool call and must be
    resolved explicitly by the caller.
    """
    explicit = os.environ.get("ASHA_HARNESS")
    if explicit:
        if explicit not in HARNESS_CHOICES:
            raise IdentityError(f"invalid ASHA_HARNESS={explicit!r}")
        return explicit

    markers: list[str] = []
    if os.environ.get("CLAUDECODE") or os.environ.get("CLAUDE_CODE_SESSION_ID"):
        markers.append("claude")
    if os.environ.get("CODEX_THREAD_ID") or os.environ.get("CODEX_MANAGED_BY_NPM"):
        markers.append("codex")
    if os.environ.get("COPILOT_CLI") or os.environ.get("COPILOT_SESSION_ID"):
        markers.append("copilot")
    if os.environ.get("OPENCODE") or os.environ.get("OPENCODE_SESSION_ID"):
        markers.append("opencode")
    unique = sorted(set(markers))
    if len(unique) > 1:
        raise IdentityError(
            "ambiguous harness markers present without ASHA_HARNESS: " + ", ".join(unique)
        )
    return unique[0] if unique else None


def resolve_identity(
    project_dir: Optional[Path] = None,
    *,
    harness: Optional[str] = None,
    session_id: Optional[str] = None,
    transcript: Optional[Path | str] = None,
    require_transcript: bool = True,
) -> SessionIdentity:
    """Resolve and validate (harness, session_id, transcript_path).

    Ordering is intentionally harness-scoped: resolve the harness first, then
    read only that harness's native session variable. This prevents nested
    Codex↔Claude calls from stealing the outer session id.
    """
    transcript_path: Optional[Path] = None
    if transcript:
        transcript_path = Path(os.path.expanduser(str(transcript)))
    elif os.environ.get("ASHA_TRANSCRIPT_PATH"):
        transcript_path = Path(os.path.expanduser(os.environ["ASHA_TRANSCRIPT_PATH"]))

    resolved_harness = harness or os.environ.get("ASHA_HARNESS")
    if resolved_harness and resolved_harness not in HARNESS_CHOICES:
        raise IdentityError(f"invalid harness={resolved_harness!r}")
    if not resolved_harness:
        if transcript_path and transcript_path.exists():
            resolved_harness = sniff_harness(transcript_path)
        if not resolved_harness:
            resolved_harness = detect_harness_from_env()
    if not resolved_harness:
        raise IdentityError("cannot resolve save harness; set ASHA_HARNESS")

    if transcript_path is None:
        transcript_path = locate_session_log(resolved_harness, project_dir)
    if transcript_path is None or not transcript_path.exists():
        if require_transcript:
            raise IdentityError(f"no transcript found for harness={resolved_harness}")
        raise IdentityError(f"no transcript found for harness={resolved_harness}")

    sniffed = sniff_harness(transcript_path)
    if sniffed and sniffed != resolved_harness:
        raise IdentityError(
            f"transcript harness mismatch: declared {resolved_harness}, file looks like {sniffed}: {transcript_path}"
        )

    resolved_sid = (
        session_id
        or os.environ.get("ASHA_SESSION_ID")
        or _native_session_id(resolved_harness)
        or transcript_session_id(transcript_path, resolved_harness)
    )
    if not resolved_sid:
        raise IdentityError(f"cannot resolve session id for harness={resolved_harness}")

    embedded_sid = transcript_session_id(transcript_path, resolved_harness)
    if embedded_sid and embedded_sid != resolved_sid:
        # An authoritative session id outranks a stale/foreign transcript
        # override. Resolve by exact id using the explicit project directory so
        # post-synthesis save_preflight sees the same auto-pin as the classifier.
        correct = (
            locate_session_log_for_id(resolved_harness, project_dir, resolved_sid)
            if project_dir is not None
            else None
        )
        if correct is None:
            raise IdentityError(
                f"session id mismatch: resolved {resolved_sid}, transcript contains "
                f"{embedded_sid}: {transcript_path}"
            )
        transcript_path = correct
        embedded_sid = transcript_session_id(transcript_path, resolved_harness)
        if embedded_sid != resolved_sid:
            raise IdentityError(
                f"exact-id transcript still mismatched: resolved {resolved_sid}, "
                f"transcript contains {embedded_sid}: {transcript_path}"
            )

    return SessionIdentity(resolved_harness, resolved_sid, transcript_path)


# ---------------------------------------------------------------------------
# locate_session_log
# ---------------------------------------------------------------------------

def _project_slug_for_claude(project_dir: Path) -> str:
    """Claude's transcript slug: cwd with '/' replaced by '-' (path-as-name)."""
    return str(project_dir.resolve()).replace("/", "-")


def locate_session_log(harness: str, project_dir: Optional[Path] = None) -> Optional[Path]:
    """Return path to the active session's native transcript, or None.

    `project_dir` defaults to $CLAUDE_PROJECT_DIR or cwd. Detection rules:

      ANY     : $ASHA_TRANSCRIPT_PATH, if set, is used verbatim and overrides all
                detection (the authoritative path from a hook payload). If set but
                missing, returns None rather than guessing another session's log.
      claude  : $CLAUDE_CODE_SESSION_ID -> ~/.claude/projects/<slug>/<sid>.jsonl
                No blind mtime fallback: if the id is known but its log is absent,
                or several logs share the slug dir, returns None to avoid pulling
                a concurrent session's transcript. Set $ASHA_ALLOW_MTIME_FALLBACK=1
                to restore the old newest-by-mtime behavior.
      codex   : $CODEX_THREAD_ID -> rollout-*<id>.jsonl, newest match first.
                Fallback: newest rollout whose session_meta cwd matches project.
                Blind newest-by-mtime fallback only when ASHA_ALLOW_MTIME_FALLBACK=1.
      copilot : scan ~/.copilot/session-state/*/inuse.<pid>.lock matching
                the parent process chain. Fallback: newest session whose
                workspace.yaml.cwd == project_dir.
      opencode: $OPENCODE_SESSION_ID -> directory JSON under OpenCode storage.
                Fallback: newest session metadata whose directory matches the
                project. Message and part records are joined during parsing.
    """
    # Authoritative override: when the caller knows the exact transcript (e.g. a
    # Stop/SessionEnd hook payload's transcript_path), honor it verbatim and skip
    # detection entirely. This is the robust fix for cross-session bleed in the
    # shared ~/.claude/projects/<slug>/ dir. Explicit-but-missing => None, never
    # a mtime guess that could belong to another session.
    explicit = os.environ.get("ASHA_TRANSCRIPT_PATH")
    if explicit:
        ep = Path(os.path.expanduser(explicit))
        return ep if ep.exists() else None

    if project_dir is None:
        env_pd = os.environ.get("CLAUDE_PROJECT_DIR") or os.environ.get("COPILOT_PROJECT_DIR")
        project_dir = Path(env_pd) if env_pd else Path.cwd()
    project_dir = project_dir.resolve()

    if harness == "claude":
        return _locate_claude(project_dir)
    if harness == "codex":
        return _locate_codex(project_dir)
    if harness == "copilot":
        return _locate_copilot(project_dir)
    if harness == "opencode":
        return _locate_opencode(project_dir)
    return None


def _locate_claude(project_dir: Path) -> Optional[Path]:
    slug = _project_slug_for_claude(project_dir)
    base = Path(os.path.expanduser(f"~/.claude/projects/{slug}"))
    if not base.exists():
        return None
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if sid:
        candidate = base / f"{sid}.jsonl"
        if candidate.exists():
            return candidate
        # Session id known but its transcript is absent: return None rather than
        # substituting another session's newest-by-mtime log (cross-session bleed).
        return None
    # Session id unknown. Only fall back when there is exactly ONE transcript in
    # the slug dir (unambiguous). Concurrent sessions share this dir, so a
    # newest-by-mtime guess routinely picks the wrong session's work. The old
    # behavior is available behind $ASHA_ALLOW_MTIME_FALLBACK=1 for recovery.
    candidates = sorted(base.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    if len(candidates) == 1:
        return candidates[0]
    if os.environ.get("ASHA_ALLOW_MTIME_FALLBACK") == "1" and candidates:
        return candidates[0]
    return None


def locate_session_log_for_id(
    harness: str,
    project_dir: Path,
    session_id: str,
) -> Optional[Path]:
    """Locate an exact session transcript without consulting override env vars.

    Drift recovery calls this after discovering that ASHA_TRANSCRIPT_PATH names
    a foreign session. Re-entering locate_session_log() would simply return the
    same override, so exact-id recovery needs a narrow, override-free path.
    """
    project_dir = project_dir.resolve()
    if harness == "claude":
        base = Path.home() / ".claude" / "projects" / _project_slug_for_claude(project_dir)
        candidate = base / f"{session_id}.jsonl"
        return candidate if candidate.exists() else None
    if harness == "codex":
        base = Path.home() / ".codex" / "sessions"
        if not base.exists():
            return None
        matches = sorted(
            base.rglob(f"rollout-*-{session_id}.jsonl"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        return matches[0] if matches else None
    if harness == "copilot":
        candidate = Path.home() / ".copilot" / "session-state" / session_id / "events.jsonl"
        return candidate if candidate.exists() else None
    if harness == "opencode":
        base = Path.home() / ".local" / "share" / "opencode" / "storage" / "session"
        matches = list(base.rglob(f"{session_id}.json")) if base.exists() else []
        return matches[0] if matches else None
    return None


def _locate_codex(project_dir: Path) -> Optional[Path]:
    base = Path(os.path.expanduser("~/.codex/sessions"))
    if not base.exists():
        return None

    # Best signal: $CODEX_THREAD_ID matches the UUID suffix in the rollout
    # filename (verified empirically 2026-05-11 — Codex's session UUID is
    # the same value it exposes via env). Falls back to cwd-match against
    # the session_meta line, then to newest rollout.
    thread_id = os.environ.get("CODEX_THREAD_ID")
    if thread_id:
        matches = sorted(
            base.rglob(f"rollout-*-{thread_id}.jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if matches:
            return matches[0]

    rollouts = sorted(base.rglob("rollout-*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    project_str = str(project_dir)
    for path in rollouts:
        try:
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                first = fh.readline()
            data = json.loads(first)
            if data.get("payload", {}).get("cwd") == project_str:
                return path
        except (OSError, json.JSONDecodeError):
            continue
    if os.environ.get("ASHA_ALLOW_MTIME_FALLBACK") == "1" and rollouts:
        return rollouts[0]
    return None


def _locate_copilot(project_dir: Path) -> Optional[Path]:
    base = Path(os.path.expanduser("~/.copilot/session-state"))
    if not base.exists():
        return None

    # Try matching the running Copilot pid via $PPID chain.
    own_pid = os.getpid()
    pid_chain: set[int] = set()
    try:
        cur = own_pid
        for _ in range(8):  # bounded walk
            stat = Path(f"/proc/{cur}/stat").read_text().split()
            ppid = int(stat[3])
            if ppid <= 1:
                break
            pid_chain.add(ppid)
            cur = ppid
    except (OSError, ValueError, IndexError):
        pass

    for session_dir in base.iterdir():
        if not session_dir.is_dir():
            continue
        for lock in session_dir.glob("inuse.*.lock"):
            try:
                lock_pid = int(lock.name.removeprefix("inuse.").removesuffix(".lock"))
            except ValueError:
                continue
            if lock_pid in pid_chain:
                events = session_dir / "events.jsonl"
                if events.exists():
                    return events

    # Fallback: newest workspace.yaml.cwd == project_dir.
    project_str = str(project_dir)
    candidates: list[tuple[float, Path]] = []
    for session_dir in base.iterdir():
        if not session_dir.is_dir():
            continue
        ws = session_dir / "workspace.yaml"
        events = session_dir / "events.jsonl"
        if not (ws.exists() and events.exists()):
            continue
        try:
            for line in ws.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("cwd:"):
                    if line.split(":", 1)[1].strip() == project_str:
                        candidates.append((events.stat().st_mtime, events))
                    break
        except OSError:
            continue
    if candidates:
        candidates.sort(reverse=True)
        return candidates[0][1]
    return None


def _locate_opencode(project_dir: Path) -> Optional[Path]:
    data_home = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share")))
    base = data_home / "opencode/storage/session"
    if not base.exists():
        return None

    sid = os.environ.get("OPENCODE_SESSION_ID") or os.environ.get("ASHA_SESSION_ID")
    if sid:
        matches = list(base.glob(f"*/{sid}.json"))
        if len(matches) == 1:
            return matches[0]
        return None

    project_str = str(project_dir)
    candidates: list[tuple[int, Path]] = []
    for path in base.glob("*/*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
            if data.get("directory") != project_str:
                continue
            updated = int((data.get("time") or {}).get("updated", 0))
            candidates.append((updated, path))
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            continue
    if candidates:
        candidates.sort(key=lambda item: item[0], reverse=True)
        return candidates[0][1]
    return None


# ---------------------------------------------------------------------------
# stream_events — three harness-specific parsers, one common Event shape
# ---------------------------------------------------------------------------

def stream_events(path: Path, harness: str) -> Iterator[Event]:
    """Stream normalized Events from a transcript.

    Unknown line types degrade to kind="meta" with a stderr warning;
    they preserve `raw` for audit. Never crashes, never silently drops.
    """
    if harness == "claude":
        parser = _parse_claude_line
    elif harness == "codex":
        parser = _parse_codex_line
    elif harness == "copilot":
        parser = _parse_copilot_line
    elif harness == "opencode":
        yield from _stream_opencode_session(path)
        return
    else:
        print(f"  WARNING: jsonl_reader: unknown harness {harness!r}", file=sys.stderr)
        return

    for lineno, line in stream_jsonl(path, label=f"{harness}-transcript"):
        try:
            yield from parser(line)
        except Exception as exc:  # noqa: BLE001
            print(
                f"  WARNING: {harness} line {lineno}: parser error ({exc}); "
                f"emitting as kind=meta",
                file=sys.stderr,
            )
            yield Event(
                timestamp=str(line.get("timestamp", "")),
                kind="meta",
                raw=line,
                detail=f"parser error: {exc}",
            )


# Claude Code transcript shape:
#   {type: "user"|"assistant"|"last-prompt"|"attachment"|"system"|...,
#    timestamp, sessionId, cwd,
#    message: {content: [{type: "text"|"tool_use"|"tool_result"|"thinking", ...}]}}
#
# Real user prompts live in `last-prompt` lines (NOT in `type=user`, which
# is exclusively tool_result echoes returned to the assistant). `last-prompt`
# repeats each time a turn finalizes; the synth-events adapter dedups by text.
def _parse_claude_line(line: dict) -> Iterator[Event]:
    line_type = line.get("type", "")
    ts = line.get("timestamp", "")

    if line_type == "last-prompt":
        text = (line.get("lastPrompt") or "").strip()
        if text:
            yield Event(timestamp=ts, kind="prompt", actor="user", text=text, raw=line)
        return

    msg = line.get("message") or {}
    content = msg.get("content") or []
    if not isinstance(content, list):
        return

    if line_type == "user":
        # Defensive: surface real text content if present (rare but possible).
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                text = (block.get("text") or "").strip()
                if text:
                    yield Event(timestamp=ts, kind="prompt", actor="user", text=text, raw=block)
        return

    if line_type == "assistant":
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "tool_use":
                # `detail` is a truncated JSON preview; consumers needing the
                # real input should use ev.raw["input"] (full, untruncated).
                yield Event(
                    timestamp=ts,
                    kind="tool_use",
                    actor="assistant",
                    tool=block.get("name") or "",
                    detail=json.dumps(block.get("input") or {}, default=str)[:1000],
                    raw=block,
                )
            elif btype == "text":
                text = (block.get("text") or "").strip()
                if text:
                    yield Event(timestamp=ts, kind="assistant_text", actor="assistant", text=text)
            # thinking blocks intentionally dropped — high noise, no synth value.
        return

    # Other line types (attachment, system, file-history-snapshot, ai-title,
    # permission-mode, queue-operation, agent-name) carry no synth-relevant
    # signal in the current vocabulary.
    return


def _millis_to_iso(value: object) -> str:
    try:
        return datetime.fromtimestamp(int(value) / 1000, timezone.utc).isoformat().replace("+00:00", "Z")
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


def _stream_opencode_session(session_path: Path) -> Iterator[Event]:
    """Join OpenCode 1.0.78+ directory JSON into normalized events.

    The session metadata file is the transcript identity boundary. Message and
    part records are loaded only from sibling storage roots and filtered by the
    embedded session/message IDs. This avoids invoking `opencode export` during
    save and keeps parsing deterministic and fixture-testable.
    """
    try:
        session = json.loads(session_path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"  WARNING: opencode session unreadable: {exc}", file=sys.stderr)
        return
    sid = str(session.get("id", ""))
    if not sid.startswith("ses_"):
        return

    storage = session_path.parents[2]
    message_dir = storage / "message" / sid
    if not message_dir.exists():
        return

    messages: list[tuple[int, dict]] = []
    for message_path in message_dir.glob("*.json"):
        try:
            message = json.loads(message_path.read_text(encoding="utf-8", errors="replace"))
            if message.get("sessionID") != sid:
                continue
            created = int((message.get("time") or {}).get("created", 0))
            messages.append((created, message))
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            continue

    for _, message in sorted(messages, key=lambda item: item[0]):
        mid = str(message.get("id", ""))
        role = message.get("role")
        ts = _millis_to_iso((message.get("time") or {}).get("created"))
        part_dir = storage / "part" / mid
        parts: list[tuple[int, dict]] = []
        if part_dir.exists():
            for part_path in part_dir.glob("*.json"):
                try:
                    part = json.loads(part_path.read_text(encoding="utf-8", errors="replace"))
                    if part.get("sessionID") != sid or part.get("messageID") != mid:
                        continue
                    start = int((part.get("time") or {}).get("start", 0))
                    parts.append((start, part))
                except (OSError, json.JSONDecodeError, TypeError, ValueError):
                    continue

        for _, part in sorted(parts, key=lambda item: item[0]):
            part_type = part.get("type")
            if role == "user" and part_type == "text" and str(part.get("text", "")).strip():
                yield Event(timestamp=ts, kind="prompt", actor="user", text=str(part["text"]).strip(), raw=part)
            elif role == "assistant" and part_type == "tool":
                state = part.get("state") or {}
                if state.get("status") not in {"completed", "running"}:
                    continue
                tool = str(part.get("tool", ""))
                tool_ts = _millis_to_iso((state.get("time") or {}).get("start")) or ts
                yield Event(
                    timestamp=tool_ts,
                    kind="tool_use",
                    actor="assistant",
                    tool=tool,
                    detail=json.dumps(state.get("input") or {}, default=str)[:1000],
                    raw={"input": state.get("input") or {}, "state": state},
                )


# Codex rollout shape:
#   {timestamp, type: "session_meta"|"event_msg"|"response_item"|"turn_context",
#    payload: {...}}
# response_item with payload.type=="message" carries role + content blocks
# (input_text for user, output_text for assistant). function_call /
# local_shell_call payloads are tool invocations.
def _parse_codex_line(line: dict) -> Iterator[Event]:
    ts = line.get("timestamp", "")
    line_type = line.get("type", "")
    payload = line.get("payload") or {}

    if line_type != "response_item":
        return

    item_type = payload.get("type", "")

    if item_type == "message":
        role = payload.get("role", "")
        content = payload.get("content") or []
        if not isinstance(content, list):
            return
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type", "")
            text = (block.get("text") or "").strip()
            if not text:
                continue
            if role == "user" and btype in ("input_text", "text"):
                # Codex injects synthetic "user" messages at session start
                # containing AGENTS.md + <environment_context> + <INSTRUCTIONS>
                # scaffold. These are not real user prompts; filter on
                # head-of-message markers (verified empirically 2026-05-11).
                head = text[:80].lstrip()
                if (head.startswith("# AGENTS.md")
                        or head.startswith("<environment_context>")
                        or head.startswith("<INSTRUCTIONS>")
                        or head.startswith("<permissions")):
                    continue
                yield Event(timestamp=ts, kind="prompt", actor="user", text=text)
            elif role == "assistant" and btype in ("output_text", "text"):
                yield Event(timestamp=ts, kind="assistant_text", actor="assistant", text=text)
            # role=="developer" / "system" are scaffold messages — skip.
        return

    if item_type in ("function_call", "local_shell_call", "shell_call", "custom_tool_call"):
        name = payload.get("name") or item_type
        # function_call: `arguments` is a JSON-encoded string
        # custom_tool_call: `input` is a raw string (e.g. apply_patch patch text)
        args = payload.get("arguments") or payload.get("input") or payload.get("action") or {}
        yield Event(
            timestamp=ts,
            kind="tool_use",
            actor="assistant",
            tool=name,
            detail=(args if isinstance(args, str) else json.dumps(args, default=str))[:1000],
            raw=payload,
        )
        return

    # function_call_output / local_shell_call_output: Codex wraps normal
    # command stdout in "Chunk ID:N / Wall time:X / Process exited..." blocks.
    # These are not errors and have no synth-relevant signal beyond what the
    # corresponding tool_use already captured. Skip.
    if item_type in ("function_call_output", "local_shell_call_output"):
        return


# Copilot events.jsonl shape (15 known event types):
#   {type, data: {...}, id, timestamp, parentId?}
# Synth-relevant: user.message, tool.execution_start, skill.invoked,
# subagent.started, tool.execution_complete (for errors).
def _parse_copilot_line(line: dict) -> Iterator[Event]:
    ts = line.get("timestamp", "")
    line_type = line.get("type", "")
    data = line.get("data") or {}

    if line_type == "user.message":
        text = (data.get("content") or data.get("text") or "").strip()
        if text:
            yield Event(timestamp=ts, kind="prompt", actor="user", text=text)
        return

    if line_type == "tool.execution_start":
        name = data.get("toolName") or data.get("tool") or ""
        # Real Copilot puts args in `arguments` (dict). `toolArgs` is null on
        # live events but kept as fallback for older / fixture shapes.
        args = data.get("arguments") or data.get("toolArgs") or data.get("args") or {}
        yield Event(
            timestamp=ts,
            kind="tool_use",
            actor="assistant",
            tool=name,
            detail=(args if isinstance(args, str) else json.dumps(args, default=str))[:1000],
            raw=data,
        )
        return

    if line_type == "tool.execution_complete":
        result = data.get("toolResult") or {}
        # Surface only for error tracking; synth pulls tool details from start event.
        if isinstance(result, dict) and (result.get("error") or result.get("resultType") == "error"):
            yield Event(
                timestamp=ts,
                kind="tool_result",
                actor="assistant",
                tool=data.get("toolName") or "",
                detail=str(result.get("error") or result.get("textResultForLlm") or "")[:500],
                raw=data,
            )
        return

    if line_type == "skill.invoked":
        cmd = data.get("skill") or data.get("command") or data.get("name") or ""
        yield Event(
            timestamp=ts,
            kind="skill",
            actor="assistant",
            tool=cmd,
            detail=str(data)[:500],
            raw=data,
        )
        return

    if line_type == "subagent.started":
        agent = data.get("agentType") or data.get("type") or data.get("name") or ""
        desc = data.get("description") or data.get("prompt") or ""
        yield Event(
            timestamp=ts,
            kind="agent",
            actor="assistant",
            tool=agent,
            detail=str(desc)[:500],
            raw=data,
        )
        return

    # session.start, session.model_change, assistant.message, assistant.turn_*,
    # subagent.completed, hook.*, system.message — no synth value today.
    return


# ---------------------------------------------------------------------------
# to_synth_events — adapt normalized Events to event_store.py dict schema
# ---------------------------------------------------------------------------

def _make_event_id(ts: str) -> str:
    """ID format mirrors event_store.py: evt_<YYYYMMDD>_<HHMMSS>_<8hex>."""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        dt = datetime.now(timezone.utc)
    return f"evt_{dt.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"


def _rel(path_str: str, project_dir: Path) -> str:
    """Strip project_dir prefix for clean event payloads."""
    project_str = str(project_dir).rstrip("/") + "/"
    if path_str.startswith(project_str):
        return path_str[len(project_str):]
    return path_str


def _shorten(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "..."


def to_synth_events(
    events: Iterable[Event],
    project_dir: Path,
    session_id: str,
) -> list[dict]:
    """Map Events to event_store.py emit-shape dicts.

    Output per dict:
      {id, timestamp, session_id, type, subtype, payload, metadata}

    Mapping (derived from post-tool-use.sh + user-prompt-submit.sh):
      prompt              -> context/decision        payload={detail, source: "user_input"}
      tool_use Edit       -> event/file_modified     payload={file_path, detail}
      tool_use MultiEdit  -> event/file_modified     payload={file_path, detail}
      tool_use Write      -> event/file_created      payload={file_path, detail}
      tool_use NotebookEdit -> event/file_modified   payload={file_path, detail}
      tool_use Task/Agent -> event/agent_deployed    payload={agent_type, description, detail}
      tool_use AskUserQuestion -> event/decision_point payload={questions, detail}
      skill (panel|save|*:*) -> event/command        payload={command, detail}
      agent               -> event/agent_deployed    payload={agent_type, description, detail}
      tool_result error   -> event/error             payload={error, context}
      assistant_text/meta -> dropped (out of synth vocabulary)
    """
    out: list[dict] = []
    project_str = str(project_dir)
    base_meta = {"source": "transcript", "project_dir": project_str}
    seen_prompts: set[str] = set()

    for ev in events:
        # Prompts often repeat in the source (Claude's last-prompt re-emits per
        # branch). Dedup by exact text; first occurrence wins.
        if ev.kind == "prompt":
            text = ev.text.strip()
            if text in seen_prompts:
                continue
            seen_prompts.add(text)

        ts = ev.timestamp or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        synth = _map_event(ev, project_dir)
        if synth is None:
            continue
        etype, subtype, payload, tool_name = synth
        out.append({
            "id": _make_event_id(ts),
            "timestamp": ts,
            "session_id": session_id,
            "type": etype,
            "subtype": subtype,
            "payload": payload,
            "metadata": {**base_meta, "tool_name": tool_name},
        })
    return out


def _map_event(
    ev: Event,
    project_dir: Path,
) -> Optional[tuple[str, str, dict, Optional[str]]]:
    """Return (type, subtype, payload, tool_name) or None to drop."""
    if ev.kind == "prompt":
        text = ev.text.strip()
        if not text:
            return None
        if len(text) <= 15 and "?" not in text:
            return None  # mirrors user-prompt-submit.sh threshold
        return ("context", "decision", {
            "detail": _shorten(text, 120),
            "source": "user_input",
        }, None)

    if ev.kind == "tool_use":
        return _map_tool_use(ev, project_dir)

    if ev.kind == "skill":
        cmd = ev.tool or ""
        if not (cmd.startswith("panel") or cmd.startswith("save") or ":" in cmd):
            return None
        return ("event", "command", {
            "command": cmd,
            "detail": f"Skill: {cmd}",
        }, "Skill")

    if ev.kind == "agent":
        agent = ev.tool or ""
        desc = ev.detail or ""
        if not agent:
            return None
        return ("event", "agent_deployed", {
            "agent_type": agent,
            "description": desc,
            "detail": f"Agent: {agent} → {desc}",
        }, "Task")

    if ev.kind == "tool_result":
        # Only the error subset survives — non-error results don't add synth value.
        err = ev.detail or ""
        if not err:
            return None
        return ("event", "error", {
            "error": _shorten(err, 200),
            "context": ev.tool or "",
        }, ev.tool)

    return None  # assistant_text, meta, etc.


def _extract_tool_args(raw: dict) -> dict | str:
    """Pull tool arguments from a harness-specific raw block.

    Three shapes seen in the wild (verified live 2026-05-11):
      - Claude tool_use         : raw["input"] is a dict
      - Copilot exec_start.data : raw["arguments"] is a dict
      - Codex function_call     : raw["arguments"] is a JSON-encoded STRING
      - Codex custom_tool_call  : raw["input"] is a raw string
                                  (e.g. apply_patch patch text)
    """
    if not isinstance(raw, dict):
        return {}
    inp = raw.get("input")
    if isinstance(inp, dict):
        return inp
    args_field = raw.get("arguments")
    if isinstance(args_field, dict):
        return args_field
    if isinstance(args_field, str):
        try:
            return json.loads(args_field)
        except json.JSONDecodeError:
            return args_field
    if isinstance(inp, str):
        return inp
    return {}


# Matches the file path on `*** Update File:`, `*** Add File:`, `*** Delete File:`
# lines in Codex's apply_patch input. Verified live 2026-05-11.
_APPLY_PATCH_UPDATE_RE = re.compile(r"\*\*\* Update File: ([^\n]+)")
_APPLY_PATCH_ADD_RE = re.compile(r"\*\*\* Add File: ([^\n]+)")
_APPLY_PATCH_DELETE_RE = re.compile(r"\*\*\* Delete File: ([^\n]+)")


def _map_tool_use(
    ev: Event,
    project_dir: Path,
) -> Optional[tuple[str, str, dict, Optional[str]]]:
    tool = ev.tool or ""
    args = _extract_tool_args(ev.raw)

    # ---- Claude vocabulary ----
    if tool in ("Edit", "MultiEdit") and isinstance(args, dict):
        fp = args.get("file_path", "")
        if not fp:
            return None
        rel = _rel(fp, project_dir)
        return ("event", "file_modified", {
            "file_path": rel,
            "detail": f"Modified: {rel}",
        }, tool)

    if tool == "Write" and isinstance(args, dict):
        fp = args.get("file_path", "")
        if not fp:
            return None
        rel = _rel(fp, project_dir)
        return ("event", "file_created", {
            "file_path": rel,
            "detail": f"Created: {rel}",
        }, tool)

    # ---- OpenCode vocabulary (1.0.78 directory storage) ----
    if tool in ("edit", "patch") and isinstance(args, dict):
        fp = args.get("filePath") or args.get("file_path") or ""
        if not fp:
            return None
        rel = _rel(fp, project_dir)
        return ("event", "file_modified", {
            "file_path": rel,
            "detail": f"Modified: {rel}",
        }, tool)

    if tool == "write" and isinstance(args, dict):
        fp = args.get("filePath") or args.get("file_path") or ""
        if not fp:
            return None
        rel = _rel(fp, project_dir)
        return ("event", "file_created", {
            "file_path": rel,
            "detail": f"Created: {rel}",
        }, tool)

    if tool == "NotebookEdit" and isinstance(args, dict):
        fp = args.get("notebook_path") or args.get("file_path") or ""
        if not fp:
            return None
        rel = _rel(fp, project_dir)
        return ("event", "file_modified", {
            "file_path": rel,
            "detail": f"Modified notebook: {rel}",
        }, tool)

    if tool in ("Task", "Agent") and isinstance(args, dict):
        agent = args.get("subagent_type") or args.get("agent_type") or ""
        desc = args.get("description") or args.get("prompt") or ""
        if not agent:
            return None
        return ("event", "agent_deployed", {
            "agent_type": agent,
            "description": desc,
            "detail": f"Agent: {agent} → {desc}",
        }, tool)

    if tool == "AskUserQuestion" and isinstance(args, dict):
        questions_blocks = args.get("questions") or []
        headers: list[str] = []
        if isinstance(questions_blocks, list):
            for q in questions_blocks:
                if isinstance(q, dict):
                    h = q.get("header") or q.get("question") or ""
                    if h:
                        headers.append(str(h))
        questions_str = ",".join(headers)
        if not questions_str:
            return None
        return ("event", "decision_point", {
            "questions": questions_str,
            "detail": f"Decision Point: {questions_str}",
        }, tool)

    if tool == "Skill" and isinstance(args, dict):
        cmd = args.get("skill") or args.get("name") or ""
        if not (str(cmd).startswith("panel") or str(cmd).startswith("save") or ":" in str(cmd)):
            return None
        return ("event", "command", {
            "command": str(cmd),
            "detail": f"Skill: {cmd}",
        }, tool)

    # ---- Codex apply_patch (custom_tool_call) ----
    # Codex's structured file-edit tool. `args` is a raw patch string.
    if tool == "apply_patch" and isinstance(args, str):
        added = _APPLY_PATCH_ADD_RE.findall(args)
        updated = _APPLY_PATCH_UPDATE_RE.findall(args)
        deleted = _APPLY_PATCH_DELETE_RE.findall(args)
        if added:
            rel = _rel(added[0].strip(), project_dir)
            more = f" (+{len(added) - 1} more)" if len(added) > 1 else ""
            return ("event", "file_created", {
                "file_path": rel,
                "detail": f"Created: {rel}{more}",
            }, tool)
        if updated:
            rel = _rel(updated[0].strip(), project_dir)
            more = f" (+{len(updated) - 1} more)" if len(updated) > 1 else ""
            return ("event", "file_modified", {
                "file_path": rel,
                "detail": f"Modified: {rel}{more}",
            }, tool)
        if deleted:
            rel = _rel(deleted[0].strip(), project_dir)
            return ("event", "file_modified", {
                "file_path": rel,
                "detail": f"Deleted: {rel}",
            }, tool)
        return None

    # ---- Copilot file ops ----
    if tool == "create" and isinstance(args, dict):
        fp = args.get("path") or args.get("file_path") or ""
        if not fp:
            return None
        rel = _rel(fp, project_dir)
        return ("event", "file_created", {
            "file_path": rel,
            "detail": f"Created: {rel}",
        }, tool)

    # Existing-file edits remain partial for Copilot. Local transcripts examined
    # through 2026-07-11 establish `create`, but contain no native edit specimen
    # from which to pin a stable tool name and argument schema. Do not guess:
    # shell-driven edits may therefore be absent from synthesized activity.

    if tool == "ask_user" and isinstance(args, dict):
        # Copilot's question-to-user tool. Args structure varies; pull whatever
        # text-ish field exists.
        question = (args.get("question")
                    or args.get("prompt")
                    or args.get("message")
                    or args.get("text")
                    or "")
        if not question:
            return None
        snippet = str(question)[:80]
        return ("event", "decision_point", {
            "questions": snippet,
            "detail": f"Asked user: {snippet}",
        }, tool)

    # ---- Tools deliberately not mapped ----
    # report_intent       Copilot internal narration; no synth value
    # exec_command/bash   shell wrappers — too varied to parse reliably,
    #                     parse from apply_patch / create when files change
    # Read/Grep/Glob/...  observational; no state change
    return None


# ---------------------------------------------------------------------------
# CLI for ad-hoc parsing / debugging
# ---------------------------------------------------------------------------

def _main(argv: list[str]) -> int:
    import argparse  # local import — keeps module-level imports tight

    p = argparse.ArgumentParser(description="Read native session transcript and emit Asha events.")
    p.add_argument("--harness", required=True, choices=tuple(sorted(HARNESS_CHOICES)))
    p.add_argument("--path", help="Override transcript path (else auto-locate).")
    p.add_argument("--project-dir", default=None)
    p.add_argument("--session-id", default=None)
    p.add_argument("--raw", action="store_true", help="Emit normalized Events instead of synth dicts.")
    args = p.parse_args(argv)

    project_dir = Path(args.project_dir).resolve() if args.project_dir else Path.cwd().resolve()
    try:
        identity = resolve_identity(
            project_dir,
            harness=args.harness,
            session_id=args.session_id,
            transcript=Path(args.path) if args.path else None,
        )
    except IdentityError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    path = identity.transcript_path

    print(f"# transcript: {path}", file=sys.stderr)
    if args.raw:
        for ev in stream_events(path, args.harness):
            print(json.dumps({
                "ts": ev.timestamp, "kind": ev.kind, "actor": ev.actor,
                "tool": ev.tool, "text": ev.text[:80], "detail": ev.detail[:80],
            }))
    else:
        events = list(stream_events(path, args.harness))
        synth = to_synth_events(events, project_dir, identity.session_id)
        for s in synth:
            print(json.dumps(s))
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
