#!/usr/bin/env python3
"""Per-beat RP continuity gate: session resolution, beat lint, routing, logging.

Standard library only and no model calls. /rp:turn runs `check` on every
draft; it decides whether a model reviewer is worth spending on the beat.

  session     print the live session file (marker first, never the clock), where
              its live state is, the day's continuity read, and any warnings
  check       lint one GM turn, route it, append the lint (and skipped) lines
  rotate      before a beat ships: move to the next fiction day's log file
  log         append one review, adjudicate or turn line to the validator log
  invariants  report live state in Memory/invariants.md and its staleness
  split       plan, read-only, how existing logs would cut into day files

The lint checks only what can be checked mechanically: the closing
Scene/Location/Present anchor, named actors against the scene's people, and
SCENE_STATE_DELTA keys and their agreement with the anchor. Everything that
needs judgement belongs to the continuity reviewer.

Current state is never read from the invariants: it lives in the session's
frontmatter and header and in the run's state file, which the session names.

One log file per fiction day: when a beat's day is later than its file's, the
file closes with a footer and the next day's file opens with the header copied
as written. A closed file is never edited again, so file:line citations hold.
"""
from __future__ import annotations

import argparse
import copy
import datetime
import errno
import glob
import itertools
import json
import os
import re
import sys
import time
from pathlib import Path

try:
    import fcntl
except ImportError:  # not POSIX: the footer append goes unlocked
    fcntl = None

SESSION_DIR = "Work/rp"
SESSION_GLOB = "rp_session_*.md"
DAY_FILE_GLOB = "day-[0-9][0-9][0-9][0-9].md"
DAY_FILE_NAME = "day-{:04d}.md"
MARKER = "Work/markers/rp-active"
VALIDATOR_LOG = "Work/rp/_validator_log.jsonl"
MODE_FILE = ".claude/modes/rp.yaml"
CANON_LAYOUT = "Memory/canon-layout.md"
INVARIANTS = "Memory/invariants.md"
DEFAULT_CHARACTER_GLOB = "Lore/World/Characters/*.md"
PRICED_RULE = Path(__file__).resolve().parents[1] / "hooks/priced-stakes/rule.json"

POLICIES = ("consequential", "every", "lint-only")
MODELS = ("haiku", "sonnet", "opus")
MAX_REWRITES = 3  # the reviewer numbers attempts 1-4
DEFAULTS = {
    "review": "consequential",
    "rewrites": {"routine": 1, "consequential": 3},
    "models": {"gm": "sonnet", "reviewer": "haiku", "adjudicator": "sonnet"},
    "cast": [],
    "deltaKeys": [],
    "consequentialPatterns": [],
}
ROTATIONS = ("day", "off")
MAX_PREVIOUS_BEATS = 200
LOG_DEFAULTS = {"rotation": "day", "previousBeats": 20}  # the sessionLog block of the mode file

# The roleplay-gm frontmatter schema. Root mirrors are derived by the command.
DELTA_KEYS = {
    "currentDate",
    "scene.time",
    "scene.location",
    "scene.participants",
    "scene.mood",
    "scene.pending_rolls",
    "scene.power_holder",
    "scene.power_method",
}
ROOT_MIRRORS = {"currentTime", "currentLocation", "participants"}
LOG_PATHS = ("review", "adjudicate", "turn")  # check writes lint and skipped
SECTIONS = ("KEEPER_INPUT", "DRAFT", "GM_SPAWN_LOG", "SOURCE_LOG", "SCENE_STATE_DELTA")

# ---------------------------------------------------------------- YAML subset
# Enough YAML for frontmatter, deltas and a mode-file block: mappings, block
# sequences, flow lists, quoted and plain scalars, comments.


def _strip_comment(line: str) -> str:
    out, quote = [], None
    for i, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'" and (i == 0 or line[i - 1] in " \t[,{:-"):
            quote = ch
        elif ch == "#" and (i == 0 or line[i - 1] in " \t"):
            break
        out.append(ch)
    return "".join(out).rstrip()


def _split_flow(text: str) -> list[str]:
    parts, buf, quote, depth = [], [], None, 0
    for ch in text:
        if quote:
            quote = None if ch == quote else quote
        elif ch in "\"'":
            quote = ch
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    if "".join(buf).strip():
        parts.append("".join(buf))
    return parts


def _scalar(text: str):
    t = text.strip()
    if t in ("", "~", "null", "Null", "NULL"):
        return None
    if t in ("true", "True"):
        return True
    if t in ("false", "False"):
        return False
    if len(t) >= 2 and t[0] == t[-1] == '"':
        try:
            return json.loads(t)
        except ValueError:
            return t[1:-1]
    if len(t) >= 2 and t[0] == t[-1] == "'":
        return t[1:-1].replace("''", "'")
    if t.startswith("[") and t.endswith("]"):
        return [_scalar(part) for part in _split_flow(t[1:-1])]
    if t.startswith("{") and t.endswith("}"):
        out = {}
        for part in _split_flow(t[1:-1]):
            key, rest = _split_key(part.strip())
            if key is not None:
                out[key] = _scalar(rest)
        return out
    if re.fullmatch(r"-?\d+", t):
        return int(t)
    return t


KEY_RE = re.compile(r"""^("[^"]*"|'[^']*'|[^\s"'\-#][^:]*?|-[^\s][^:]*?)\s*:(?:\s+(.*)|)$""")


def _split_key(text: str):
    m = KEY_RE.match(text)
    if not m:
        return None, None
    key = m.group(1)
    if key[:1] in "\"'":
        key = key[1:-1]
    return key, (m.group(2) or "")


def _is_item(text: str) -> bool:
    return text == "-" or text.startswith("- ")


def _block(rows, i, indent):
    return _seq(rows, i, indent) if _is_item(rows[i][1]) else _map(rows, i, indent)


def _map(rows, i, indent):
    out = {}
    while i < len(rows) and rows[i][0] >= indent:
        level, text = rows[i]
        if level > indent or _is_item(text):
            if level == indent:
                break
            i += 1  # stray deeper line: skip it
            continue
        key, rest = _split_key(text)
        i += 1
        if key is None:
            continue
        if rest.strip() in ("|", ">", "|-", ">-"):
            lines = []
            while i < len(rows) and rows[i][0] > indent:
                lines.append(rows[i][1])
                i += 1
            out[key] = "\n".join(lines)
        elif rest.strip():
            out[key] = _scalar(rest)
        elif i < len(rows) and (rows[i][0] > indent or (rows[i][0] == indent and _is_item(rows[i][1]))):
            out[key], i = _block(rows, i, rows[i][0])
        else:
            out[key] = None
    return out, i


def _seq(rows, i, indent):
    out = []
    while i < len(rows) and rows[i][0] == indent and _is_item(rows[i][1]):
        text = rows[i][1]
        item = text[1:].strip()
        i += 1
        if not item:
            if i < len(rows) and rows[i][0] > indent:
                value, i = _block(rows, i, rows[i][0])
                out.append(value)
            else:
                out.append(None)
            continue
        key, _rest = _split_key(item)
        if key is not None and item[:1] not in "\"'[":
            child = indent + len(text) - len(item)
            sub = [(child, item)]
            while i < len(rows) and rows[i][0] > indent:
                sub.append(rows[i])
                i += 1
            value, _ = _map(sub, 0, child)
            out.append(value)
        else:
            out.append(_scalar(item))
    return out, i


def parse_yaml(text: str):
    rows = []
    for raw in text.splitlines():
        if raw.strip().startswith("```"):
            continue
        line = _strip_comment(raw.replace("\t", "  "))
        if line.strip():
            rows.append((len(line) - len(line.lstrip(" ")), line.strip()))
    if not rows:
        return None
    value, _ = _block(rows, 0, rows[0][0])
    return value


def read_frontmatter(path: Path) -> dict:
    """Parse the leading YAML frontmatter without reading the session body."""
    lines = []
    try:
        with open(path, encoding="utf-8-sig", errors="replace") as fh:
            if fh.readline().strip() != "---":
                return {}
            for n, line in enumerate(fh):
                if line.strip() == "---":
                    break
                if n > 2000:
                    return {}
                lines.append(line)
            else:
                return {}
    except OSError:
        return {}
    data = parse_yaml("".join(lines))
    return data if isinstance(data, dict) else {}


# ------------------------------------------------------------ session file


def _marker_fields(project: Path) -> dict:
    try:
        text = (project / MARKER).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    fields = {}
    for line in text.splitlines():
        key, rest = _split_key(line.strip())
        if key and rest and key not in fields:
            fields[key] = _scalar(rest)
    return fields


def _marker_session(project: Path):
    value = _marker_fields(project).get("session_file")
    return value if isinstance(value, str) and value else None


def _inside_sessions(project: Path, name: str):
    candidate = (project / name).resolve()
    sessions = (project / SESSION_DIR).resolve()
    if candidate.suffix != ".md" or sessions not in candidate.parents:
        return None
    return Path(os.path.normpath(project / name))


def _relative(project: Path, path: Path) -> str:
    return Path(os.path.relpath(path, project)).as_posix()


def resolve_session_file(project, today: str | None = None, config=None) -> dict:
    """The live session file: the marker's record, else the newest open session.

    The clock never names it: one session can run for days. A file closed by
    rotation is followed forward to the day file that continues it.
    """
    project = Path(project)
    today = today or datetime.date.today().isoformat()
    warning = ""
    named = _marker_session(project)
    if named:
        path = _inside_sessions(project, named)
        if path and path.is_file():
            head, hops = _follow_rotation(project, path)
            rel, lost = _relative(project, head), rotation_footer(head)
            warning = f"rp-active names {named}, which rotation closed; following it to {rel}." if hops else None
            if lost:
                warning = (f"{rel} was closed by rotation into {lost}, which is missing; do not write to {rel}. "
                           "Ask the Keeper.")
            return {"path": rel, "source": "marker", "warning": warning}
        warning = f"rp-active names {named}, which is not a session file under {SESSION_DIR}/. "
    else:
        warning = "rp-active does not name the session file. "
    rel = _newest_open(project)
    if rel:
        return {
            "path": rel,
            "source": "latest",
            "warning": warning + f"Using the newest open session, {rel}. /rp:start records it in the marker.",
        }
    closed = _closed_but_rotatable(project, config)
    if closed:  # /rp:end closed it before its day rotated: continue the run from it, never from a hand-made day file
        warning += (f"{closed} was closed by /rp:end but can still rotate into its day files: to continue that run, "
                    f"open it with /rp:start load {closed} and the opening fiction day (rotate --start --day N). A "
                    "later day closes it with a footer and opens that day's file; on its own day it reopens the log, "
                    "and play goes on there until the day moves. ")
    rel = f"{SESSION_DIR}/rp_session_{today}.md"
    if (project / rel).is_file():  # today's name is taken (a closed log, or one the fallback passes over)
        return {"path": rel, "source": "new", "warning": warning + (
            f"No open session file is offered, but {rel} already exists: /rp:start load {rel} to continue it (a closed "
            "log reopens); never write over it.")}
    return {"path": rel, "source": "new", "warning": warning + f"No session file exists yet; a new one would be {rel}."}


def _closed_but_rotatable(project: Path, config=None):
    """The newest legacy log when /rp:end closed it (status: closed) and rotation could still continue it:
    no footer, not continued by a day file in its log_dir, a valid log_dir, a fiction day, rotation on."""
    root = project / SESSION_DIR
    newest = max(root.glob(SESSION_GLOB), key=lambda p: p.name, default=None)
    if newest is None or rotation_footer(newest):
        return None
    data = read_frontmatter(newest)
    if str(data.get("status", "")).lower() != "closed" or _log_dir(project, data)[0] is None:
        return None
    config = config or load_config(project)
    if config["log"]["rotation"] == "off" or _file_day(data, _read_lines(newest)) is None:
        return None
    rel = _relative(project, newest)
    continued = any(_continues_target(project, read_frontmatter(p)) == rel for p in root.glob("**/" + DAY_FILE_GLOB)
                    if _in_its_log_dir(project, p))  # a backup copy elsewhere continues nothing
    return None if continued else rel


def _newest_open(project: Path):
    """The newest open session: a legacy log not closed, or the head of a day-file chain.

    A day file is closed only by its rotation footer; /rp:end leaves it open for
    the next session to continue. A file another open day file continues is not
    a head, even unfooted (a rotation interrupted before its footer).
    """
    root = project / SESSION_DIR
    every_day = [p for p in sorted(root.glob("**/" + DAY_FILE_GLOB))
                 if p.is_file() and _in_its_log_dir(project, p)]  # a copy outside its log_dir (a backup) is not one
    continued = {_continues_target(project, read_frontmatter(p)) for p in every_day}
    days = [p for p in every_day if not rotation_footer(p)]
    # Only the newest legacy log can be open: no older one was ever closed (status: closed came with
    # /rp:end in rp 0.4.0), and campaign names changed within one run. A closed newest log, or one
    # that moved to day files (footered, or continued by one), leaves no legacy log open.
    newest = max(root.glob(SESSION_GLOB), key=lambda p: p.name, default=None)
    legacy = [newest] if newest and not rotation_footer(newest) and _relative(project, newest) not in continued \
        and str(read_frontmatter(newest).get("status", "")).lower() != "closed" else []
    if not days:
        return _relative(project, legacy[0]) if legacy else None
    heads = [p for p in days + legacy if _relative(project, p) not in continued] or days
    return _relative(project, max(heads, key=lambda p: (p.stat().st_mtime_ns, p.name)))


def _in_its_log_dir(project: Path, path: Path) -> bool:
    data = read_frontmatter(path)
    log_dir, _warning = _log_dir(project, data)
    return data.get("log_day") is not None and log_dir == _relative(project, path.parent)


# -------------------------------------------------------------- live state
# The run's current state lives in the session's frontmatter and header and in
# the run's state file, which the session names (frontmatter run_state:, else a
# "State: `path` § Section" line in the header). The gate reports line ranges;
# the GM and the reviewer read them. Nothing here reads the invariants.

RUN_STATE_SECTION = "Run state"
HEADER_MAX_LINES = 250
HEADER_END_RE = re.compile(r"^#{1,6}\s+(?:(?i:day plan\b|beat\s+\d+\b)|B\d+\b)")  # "### B4100 — Day 195"
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})(.*)$")
# "State: `path` § Section" or "Run state: `path`", never "Scene state: `map.md`".
STATE_POINTER_RE = re.compile(
    r"(?i)(?<![\w`])(?:\*\*)?(?:(\w+)\s+)?state(?:\*\*)?\s*:\s*(?:\*\*)?\s*`([^`\n]+?\.md)`(?:\s*§\s*([^()\n.;`]+))?"
)
DATE_RE = re.compile(r"(?<![\w-])(\d{4}-\d{2}-\d{2})(?!\d)")  # 2026-10-24T10:00 too; not rp_session_2026-10-17
DAY_RE = re.compile(r"\bDay\s+(\d+)\b")
BEAT_RE = re.compile(r"\bBeat\s+(\d+)\b")
SESSION_DATE_RE = re.compile(r"rp_session_(\d{4}-\d{2}-\d{2})")


def _heading_text(text: str) -> str:
    text = re.sub(r"(?i)\[KEEPER-LOCKED\]", " ", text)
    text = re.sub(r"[`*_~⚑]", " ", text)
    text = re.sub(r"^\s*\d+(?:\.\d+)*[.)]?\s+", "", text)  # "3. Timeline State"
    return " ".join(text.split()).strip(" .:-—–").lower()


def _iso(value) -> str | None:
    text = str(value or "").strip()
    try:
        return datetime.date.fromisoformat(text).isoformat() if DATE_RE.fullmatch(text) else None
    except ValueError:
        return None


def _fenced(lines: list[str]) -> set[int]:
    """Indices of lines inside closed code fences. An unclosed fence hides nothing."""
    inside, i = set(), 0
    while i < len(lines):
        m = FENCE_RE.match(lines[i])
        if m and m.group(1)[0] not in m.group(2):  # ```yaml opens; ```inline``` does not
            closing = re.compile(r"^\s{0,3}%s{%d,}\s*$" % (re.escape(m.group(1)[0]), len(m.group(1))))
            close = next((j for j in range(i + 1, len(lines)) if closing.match(lines[j])), None)
            if close is not None:
                inside.update(range(i, close + 1))
                i = close + 1
                continue
        i += 1
    return inside


def _session_layout(path: Path) -> dict:
    """Line ranges of the frontmatter and the header (up to the Day Plan or first beat)."""
    layout = {"frontmatter": None, "header": None, "text": []}
    try:
        fh = open(path, encoding="utf-8-sig", errors="replace")
    except OSError:
        return layout
    with fh:
        numbered = enumerate(fh, 1)
        first = next(numbered, None)
        if first is None:
            return layout
        pending = [first]
        if first[1].strip() == "---":
            pending = []
            for n, line in numbered:
                if line.strip() == "---":
                    layout["frontmatter"] = {"start": 1, "end": n}
                    break
                if n > 2000:
                    return layout
            else:
                return layout
        start = layout["frontmatter"]["end"] + 1 if layout["frontmatter"] else 1
        end, ended_by, truncated = start - 1, "eof", False
        for n, line in itertools.chain(pending, numbered):  # lazily: a log runs to megabytes
            if HEADER_END_RE.match(line):
                ended_by = line.strip()[:80]
                break
            if n - start + 1 > HEADER_MAX_LINES:
                truncated = True
                break
            end = n
            layout["text"].append(line.rstrip("\n"))
    if end >= start:
        layout["header"] = {"start": start, "end": end, "ended_by": ended_by, "truncated": truncated}
    return layout


def _state_pointer(data: dict, header: list[str]):
    """(pointer, warning): where the session says the run's state file is."""
    value = data.get("run_state")
    if value is not None and not isinstance(value, str):
        return None, "run_state in the session frontmatter must be one quoted \"<path>#<section>\" string."
    if isinstance(value, str) and value.strip():
        m = re.match(r"\s*(.+?\.md)\s*(?:[#§]\s*(.*?))?\s*$", value)
        if not m:
            return None, f"run_state {value!r} names no .md file."
        return {"raw": m.group(1), "section": (m.group(2) or "").strip() or RUN_STATE_SECTION,
                "source": "frontmatter"}, None
    for m in STATE_POINTER_RE.finditer("\n".join(header)):
        if m.group(1) is None or m.group(1).lower() == "run":
            return {"raw": m.group(2), "section": (m.group(3) or "").strip() or RUN_STATE_SECTION,
                    "source": "header"}, None
    return None, ("The session names no run state file: add run_state: \"<path>#<section>\" to its "
                  "frontmatter (/rp:start writes it). Current state comes from the session alone.")


def _inside_project(project: Path, raw: str):
    """The file a pointer names, if it stays inside the project (lexically) and is markdown."""
    root = os.path.abspath(project)
    path = os.path.abspath(os.path.join(root, raw))
    if not path.endswith(".md") or path == root or os.path.commonpath([root, path]) != root:
        return None
    return Path(path)


def _section(path: Path, name: str):
    """A heading's line range and its first entry (the current one by convention)."""
    try:
        lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    except OSError:
        return None
    fenced = _fenced(lines)
    headings = [(i, len(m.group(1)), _heading_text(m.group(2))) for i, m in
                ((i, HEADING_RE.match(line)) for i, line in enumerate(lines) if i not in fenced) if m]
    want = _heading_text(name)
    exact = re.compile(r"%s(?:\s*[(\[—–:-]|$)" % re.escape(want))  # "Run state (one page)", not "... history"
    loose = re.compile(r"%s(?!\w)" % re.escape(want))
    chosen = next((h for h in headings if exact.match(h[2])), None) or next(
        (h for h in headings if loose.match(h[2])), None)
    if chosen is None:
        return None
    start, level = chosen[0], chosen[1]
    end = next((i for i, lvl, _ in headings if i > start and lvl <= level), len(lines))
    j = start + 1
    while j < end and not lines[j].strip():
        j += 1
    k = j
    while k < end and lines[k].strip() and lines[k].strip() != "---" and not HEADING_RE.match(lines[k]):
        k += 1
    current = None
    if k > j:
        text = " ".join(lines[j:k])
        day, beat = DAY_RE.search(text), BEAT_RE.search(text)
        current = {
            "start": j + 1,
            "end": k,
            "day": int(day.group(1)) if day else None,
            "time": _norm_time(text),
            "beat": int(beat.group(1)) if beat else None,
            "dates": sorted({d for d in DATE_RE.findall(text) if _iso(d)}),
        }
    return {"heading_line": start + 1, "end_line": end, "current": current}


def live_state(project, session_file, today: str | None = None) -> dict:
    """Where the run's current state is: line ranges, the run state's current entry, its date."""
    project = Path(project)
    today = today or datetime.date.today().isoformat()
    path = Path(session_file)
    path = path if path.is_absolute() else project / path
    layout = _session_layout(path)
    data = read_frontmatter(path)
    scene = data.get("scene") if isinstance(data.get("scene"), dict) else {}
    warnings, run_state = [], None
    pointer, warning = _state_pointer(data, layout["text"])
    if warning:
        warnings.append(warning)
    if pointer:
        target = _inside_project(project, pointer["raw"])
        if target is None:
            warnings.append(f"run state {pointer['raw']} is outside the project or not a .md file; not read.")
        elif target == _inside_project(project, INVARIANTS):
            warnings.append(f"run state {pointer['raw']} names the invariants; current state is never read "
                            "from them.")
        elif not target.is_file():
            warnings.append(f"run state {pointer['raw']} does not exist.")
        else:
            section = _section(target, pointer["section"])
            if section is None:
                warnings.append(f"{_relative(project, target)} has no '{pointer['section']}' section.")
            else:
                run_state = {"path": _relative(project, target), "section": pointer["section"],
                             "source": pointer["source"], **section}
    day = DAY_RE.search(str(data.get("currentDate") or ""))
    # The run's state is dated by its current entry. A session opening on a later
    # day changes nothing static, so its start date stands in only without one.
    dates = [d for d in (run_state["current"]["dates"] if run_state and run_state["current"] else []) if d <= today]
    started = SESSION_DATE_RE.search(path.name)
    started = _iso(started.group(1)) if started else _iso(data.get("opened"))  # a day file: the day it opened
    if dates:
        state_date, source = max(dates), "run_state"
    elif started and started <= today:
        state_date, source = started, "session"
    else:
        state_date, source = None, None
    return {
        "session_file": str(session_file),
        "frontmatter": layout["frontmatter"],
        "header": layout["header"],
        "as_of": {"day": int(day.group(1)) if day else None,
                  "time": _norm_time(scene.get("time", data.get("currentTime")))},
        "run_state": run_state,
        "state_date": state_date,
        "state_date_source": source,
        "warnings": warnings,
    }


# A snapshot of state in the invariants: a section of that name, a bold label
# opening a line ("**Run state (Day 60)**:"), or a phrase only a snapshot uses.
# Protocol words ("Scene state", "Current state: read the session") are not.
HEADING_NAMES = ("timeline state", "run state", "live state", "current state", "live clocks",
                 "current vessel", "resume point")
LABEL_NAMES = ("timeline state", "run state", "live clocks", "current vessel", "resume point")
HEADING_NAME_RE = re.compile(r"(?:%s)(?!\w)" % "|".join(HEADING_NAMES))
LIVE_LABEL_RE = re.compile(
    r"(?i)^\s*(?:[-*+]\s+|>\s*)?(?:"
    r"\*\*\s*(?:{0})\s*(?:\([^)\n]*\))?\s*(?::\s*\*\*|\*\*\s*(?:\([^)\n]*\)|`[^`\n]*`)?\s*:)"
    r"|(?:{0})\s*(?:\([^)\n]*\))?\s*:)".format("|".join(LABEL_NAMES))
)
SNAPSHOT_RE = re.compile(
    r"(?i:\bnow\s+at\s+day\s+\d+|\bresumes\s+(?:at\s+)?beat\s+\d+)"
    r"|(?i:\bnow\s+at)\s+D\d+\b|(?i:\bresumes\s+(?:at\s+)?)B\d+\b"  # the logs' D195, B4100
)


def _live_locked_name(name: str) -> bool:
    return any(HEADING_NAME_RE.match(_heading_text(part)) for part in re.split(r"→|->|>", name))


def invariants_report(project, state: dict | None) -> dict | None:
    """Live state in Memory/invariants.md, and whether it predates the run's state.

    Each hit says whether it sits in KEEPER-LOCKED content: a section whose heading
    carries the marker or is named in keeper_locked_sections, a line carrying the
    marker, or the frontmatter list itself. Locked content is the Keeper's to retire.
    """
    path = Path(project) / INVARIANTS
    try:
        lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    except OSError:
        return None
    body = 0
    if lines and lines[0].strip() == "---":
        body = next((i + 1 for i in range(1, min(len(lines), 2000)) if lines[i].strip() == "---"), 0)
    data = parse_yaml("\n".join(lines[1:body - 1])) if body else None
    data = data if isinstance(data, dict) else {}
    hits = []
    names = [n for n in data.get("keeper_locked_sections") or [] if isinstance(n, str)] \
        if isinstance(data.get("keeper_locked_sections"), list) else []
    listed = {_heading_text(re.split(r"→|->|>", n)[-1]) for n in names}
    for name in names:
        if _live_locked_name(name):
            n = next((i + 1 for i in range(body) if name in lines[i]), 1)
            hits.append({"line": n, "kind": "locked", "locked": True, "text": name[:100]})
    fenced = _fenced(lines)
    locked_level = None
    for n, line in enumerate(lines[body:], body + 1):
        if n - 1 in fenced:
            continue
        heading = HEADING_RE.match(line)
        marked = "[keeper-locked]" in line.lower()
        if heading:
            level, name = len(heading.group(1)), _heading_text(heading.group(2))
            if locked_level is not None and level <= locked_level:
                locked_level = None
            if locked_level is None and (marked or name in listed):
                locked_level = level
            kind = "heading" if HEADING_NAME_RE.match(name) else None
        else:
            kind = "label" if LIVE_LABEL_RE.match(line) else "snapshot" if SNAPSHOT_RE.search(line) else None
        if kind:
            hits.append({"line": n, "kind": kind, "locked": locked_level is not None or marked,
                         "text": line.strip()[:100]})
    state = state or {}
    state_date, source = state.get("state_date"), state.get("state_date_source")
    warnings = []
    if hits:
        where = ", ".join(str(h["line"]) for h in hits[:8]) + (", ..." if len(hits) > 8 else "")
        day = (state.get("as_of") or {}).get("day")
        locked = any(h["locked"] for h in hits)
        warnings.append({
            "kind": "live_state",
            "detail": f"{INVARIANTS} carries live state (lines {where})"
                      + (f"; the run is at Day {day}" if day else "")
                      + ". The gate reads current state from the session and the run's state file; "
                        "re-run /rp:extract-invariants, which writes static canon only."
                      + (" Some of it is KEEPER-LOCKED: the Keeper retires it first." if locked else ""),
            "locked": locked,
            "lines": [h["line"] for h in hits],
            "hits": hits,
        })
    extracted = _iso(data.get("last_extraction"))
    if not extracted:
        warnings.append({"kind": "no_extraction_date",
                         "detail": f"{INVARIANTS} has no last_extraction date; its age against the run's "
                                   "state is unknown."})
    elif state_date and extracted < state_date:
        warnings.append({"kind": "stale",
                         "detail": f"{INVARIANTS} was extracted {extracted}, which predates the run's state of "
                                   f"{state_date} (from the {(source or 'session').replace('_', ' ')}); canon "
                                   "ratified since then is missing. Re-run /rp:extract-invariants."})
    return {"path": INVARIANTS, "last_extraction": extracted, "state_date": state_date,
            "state_date_source": source, "warnings": warnings}


# -------------------------------------------------------------- turn input

SECTION_RE = re.compile(r"^\s*(?:#+\s*)?(?:\*\*)?(%s)(?:\*\*)?\s*:(?:\*\*)?\s*(.*)$" % "|".join(SECTIONS))


def parse_turn(text: str) -> dict:
    """Split a GM turn file into its blocks. Text before any header is the draft."""
    sections, preamble, current, buf = {}, [], None, []
    for line in text.splitlines():
        if line.strip().startswith("```"):
            continue
        m = SECTION_RE.match(line)
        if m:
            if current:
                sections[current] = "\n".join(buf).strip()
            current, rest = m.group(1), m.group(2).strip()
            buf = [] if rest in ("", "|", ">") else [rest]
        elif current:
            buf.append(line)
        else:
            preamble.append(line)
    if current:
        sections[current] = "\n".join(buf).strip()
    sections.setdefault("DRAFT", "\n".join(preamble).strip())
    for name in SECTIONS:
        sections.setdefault(name, "")
    return sections


# ----------------------------------------------------------------- people

TITLES = {"The", "A", "An", "Dr", "Mr", "Mrs", "Ms", "Mx", "Miss", "Lady", "Lord", "Sir", "Madam", "Master",
          "Mistress", "Old", "Young", "Saint", "St", "Mother", "Father", "Brother", "Sister", "Aunt", "Uncle",
          "Capt", "Cpt", "Captain", "Col", "Gen", "Lt", "Sgt", "Cpl", "Maj", "Adm", "Fr", "Rev", "Prof", "Hon",
          "Mme", "Mlle",
          # pronouns and determiners are never names, even capitalised
          "He", "She", "It", "They", "We", "You", "I", "Him", "Her", "Them", "Us", "Me", "His", "Hers", "Its",
          "Their", "Theirs", "Our", "Ours", "Your", "Yours", "My", "Mine", "This", "That", "These", "Those"}
TOKEN_RE = re.compile(r"[^\W\d_][\w'’-]*")
NOBODY = {"", "none", "nobody", "no one", "noone", "alone", "empty", "-", "etc", "others"}
ABBREVIATIONS = ("Mr|Mrs|Ms|Mx|Dr|St|Mt|Jr|Sr|Capt|Cpt|Col|Gen|Lt|Sgt|Cpl|Maj|Adm|Fr|Rev|Prof|Hon|Gov|Sen"
                 "|Mme|Mlle|Br|Sgt")


def name_tokens(name: str) -> set[str]:
    tokens = set()
    for raw in TOKEN_RE.findall(re.sub(r"\([^()]*\)", " ", name)):
        token = raw.strip("'’-")
        if token[:1].isupper() and token.rstrip(".") not in TITLES and len(token) >= 2:
            tokens.add(token.lower())
    return tokens


def _plain(name: str) -> str:
    text = re.sub(r"\([^()]*\)", " ", name).lower().replace(".", " ")
    return " ".join(w for w in text.split() if w not in ("the", "a", "an"))


def same_person(a: str, b: str) -> bool:
    """Marr is Ildric Marr; Odo Brand is not Hesta Brand; Mother is Mother."""
    left, right = name_tokens(a), name_tokens(b)
    if not left or not right:
        return bool(_plain(a)) and _plain(a) == _plain(b)
    return left <= right or right <= left


def same_people(left: list[str], right: list[str]) -> bool:
    return all(any(same_person(x, y) for y in right) for x in left) and all(
        any(same_person(y, x) for x in left) for y in right
    )


def present_names(value: str) -> list[str]:
    text = value
    while True:
        stripped = re.sub(r"\([^()]*\)", "", text)
        if stripped == text:
            break
        text = stripped
    text = re.sub(r"\b(%s|[A-Z])\." % ABBREVIATIONS, lambda m: m.group(1) + "\u2024", text)  # "Mr. Holt", "J. Rook"
    text = re.split(r"\.(?:\s|$)", text)[0]  # a field holds names, not sentences
    parts = (part.strip(" .*`—–-").replace("\u2024", ".") for part in re.split(r"[,;]|\band\b|&|[—–]", text))
    return [part for part in parts if part and ":" not in part and _plain(part) not in NOBODY]


def _participant_names(value) -> list[str]:
    names = []
    for item in value if isinstance(value, list) else []:
        if isinstance(item, dict) and item.get("name"):
            names.append(str(item["name"]))
        elif isinstance(item, str) and item.strip():
            names.append(item.strip())
    return names


def load_scene(project, session_file) -> dict:
    """Scene state at the start of the turn, from the session frontmatter."""
    project = Path(project)
    path = Path(session_file)
    data = read_frontmatter(path if path.is_absolute() else project / path)
    scene = data.get("scene") if isinstance(data.get("scene"), dict) else {}
    raw = scene.get("participants") or data.get("participants") or []
    raw = raw if isinstance(raw, list) else []
    pc, pc_role = None, None
    for item in raw:
        if isinstance(item, dict) and str(item.get("role", "")).upper().startswith("PC"):
            pc, pc_role = str(item.get("name", "")), str(item.get("role", ""))
            break
    warning = None
    if not data:
        warning = f"{session_file} has no readable YAML frontmatter; the scene starts empty."
    elif not raw:
        warning = f"{session_file} frontmatter lists no participants."
    return {
        "participants": _participant_names(raw),
        "pc": pc,
        "pc_role": pc_role,
        "time": scene.get("time", data.get("currentTime")),
        "day": fiction_day(data.get("currentDate")),
        "log_day": _log_day(data),
        "warning": warning,
    }


def _character_globs(project: Path) -> list[str]:
    try:
        text = (project / CANON_LAYOUT).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return [DEFAULT_CHARACTER_GLOB]
    m = re.search(r"(?m)^\|\s*`?character`?\s*\|([^|\n]+)\|", text)
    globs = re.findall(r"`([^`]+)`", m.group(1)) if m else []
    return globs or [DEFAULT_CHARACTER_GLOB]


def _cast(project: Path, config: dict, people: list[str]) -> list[str]:
    """Names to look for: the scene's people, turnGate.cast, then character-file stems."""
    cast = list(people) + list(config["cast"])
    for pattern in _character_globs(project):
        try:
            matches = glob.glob(os.path.join(project, pattern), recursive=True)
        except (ValueError, OSError):
            continue
        cast.extend(Path(path).stem for path in matches if os.path.isfile(path))
    return cast


# ------------------------------------------------------------------- lint

QUOTE_RE = re.compile(r'(?<![\w])["“][^"“”\n]*["”]')  # closed pairs only; 12" is not a quote
ANCHOR_START_RE = re.compile(r"(?i)\**\s*\bscene\s*:")
SENTENCE_RE = re.compile(r"(?<=[.!?])\s+|\n+|;\s*")
ANCHOR_LINE_RE = re.compile(r"(?i)\bscene\s*:.*\|.*\bpresent\s*:")
TIME_RE = re.compile(r"~?\b(\d{1,2}):(\d{2})\b")
# A possession that does not put its owner on the page: "Velathra's plans", "Vey's letter".
ARTIFACTS = {"letter", "letters", "note", "notes", "plans", "plan", "book", "books", "ledger", "page", "pages",
             "name", "names", "word", "words", "yard", "house", "home", "room", "shop", "farm", "mill", "grave",
             "things", "belongings", "memory", "ghost", "absence", "debt", "debts", "money", "coin", "coins",
             "purse", "cart", "horse", "dog", "bed", "stool", "chair", "place", "stall", "account", "accounts",
             "seal", "handwriting", "portrait", "picture", "story", "tale", "song", "promise", "orders", "terms",
             "price", "offer", "rule", "rules", "working", "workings", "door", "gate", "land", "fields", "people"}
SPEECH_WORDS = {"says", "said", "asks", "asked", "calls", "called", "answers", "answered", "adds", "added",
                "tells", "told", "whispers", "whispered", "murmurs", "murmured", "replies", "replied",
                "shouts", "shouted", "snaps", "snapped", "mutters", "muttered"}
ARTICLES = {"the", "a", "an"}
MENTION_PREPS = {"without", "whether"}
FAMILY = r"\s+(?:brothers|sisters|family|household|boys|girls|twins|clan|farm|house|place|name|kind|way)\b"
# "of Vey" and "from Vey" are mentions only after these: "thinks of Vey", "a letter from Vey".
OF_MENTION = {"think", "thinks", "thought", "thinking", "speak", "speaks", "spoke", "talk", "talks", "talked",
              "hear", "hears", "heard", "dream", "dreams", "dreamt", "dreamed", "remind", "reminds", "reminded",
              "memory", "memories", "word", "news", "story", "stories", "name", "mention", "sign", "trace",
              "nothing", "none", "rest", "tired", "sick", "fond", "afraid", "jealous", "proud", "ashamed"}
FROM_MENTION = {"word", "letter", "note", "message", "news", "gift", "present", "order", "orders", "request",
                "greetings", "money", "coin", "payment"}
# Belief, not speech: "Liss knows Vey hates the cold" is not Vey acting.
REPORT = {"knows", "knew", "thinks", "thought", "believes", "believed", "remembers", "remembered", "hopes",
          "hoped", "wonders", "wondered", "fears", "feared", "imagines", "imagined", "doubts", "doubted",
          "suspects", "suspected", "guesses", "guessed"}
PRONOUNS = {"he", "she", "they", "i", "we", "you", "it"}
GONE_RE = re.compile(r"\s+(?:(?:is|was|has\s+been|had\s+been)\s+(?:still\s+)?(?:gone|away|absent|elsewhere)"
                     r"|(?:has|had)\s+(?:gone|left))\b")
LATER_RE = re.compile(r"\s+(?:(?:will|would|should|may|might|must|could)\s+be\s+(?:back|home|gone|away|out)"
                      r"|used\s+to\s+(?!(?:the|a|an|it|his|her|their|this|that)\b)[a-z]+)\b")  # habit, not "used to the weight"
SPEAKER_RE = re.compile(r"\s*(?:\([^()]{0,40}\))?\s*(?::|[—–]|--)\s*«Q»")
ARRIVAL_RE = re.compile(
    r"(?i)\b(?:comes?|came|coming|walks?|walked|steps?|stepped|hurries|hurried|runs?|ran|rides?|rode"
    r"|climbs?|climbed|strides?|strode|limps?|limped|ducks?|ducked)\s+(?:back\s+)?"
    r"(?:in|into|inside|through|up\s+the|down\s+the|over\s+to|across\s+the)\b"
    r"|\b(?:arriv(?:es|ed|ing)?|enters|entered|entering)\b|\bin\s+from\s+the\b"
    r"|\b(?:returns?|returned|cross(?:es|ed)?|back)\s+(?:over\s+)?from\b|\b(?:is|was)\s+back\b"
    r"|\bthrough\s+the\s+(?:door|doorway|gate)\b|\bappears?\s+(?:in|at)\s+the\s+(?:door|doorway|gate)\b"
    r"|\b(?:comes?|came)\s+to\s+the\s+(?:\w+\s+)?(?:door|doorway|gate|threshold)\b"
)
FIGHT_RE = re.compile(
    r"(?i)\b(?:attack(?:s|ed|ing)?|punch(?:es|ed|ing)?|stab(?:s|bed|bing)?|slash(?:es|ed|ing)?"
    r"|grappl\w+|wrestl\w+|kill(?:s|ed|ing)?|captur\w+|unconscious"
    r"|(?:fights?|fought|fighting|strikes?|struck|slaps?|slapped|wounds?|seiz(?:e|es|ed|ing)|grabs?|grabbed"
    r"|pins?|pinned|binds?|bound|ties|tied|tying|shackles?|shackled|manacles?|manacled|chains|chained|shoots?|shot)"
    r"\s+(?:him|her|them)(?!\s+(?:stride|best|way|turn|own)\b)|knock(?:s|ed)?\s+(?:him|her|them)\s+(?:down|out))\b"
)


def _quote(m) -> str:
    """A quotation becomes a placeholder; one that ends a sentence still ends it."""
    inner = m.group(0)[:-1].rstrip()
    return " «Q». " if inner.endswith((".", "!", "?", "…")) else " «Q» "


def _excused(sentence: str, start: int, end: int, tokens_re) -> str | None:
    """Why a mention of an absent cast member is not them on the page, or None.

    Every other mention counts: a false flag costs one cheap review, a miss ships
    unreviewed. Each excuse is narrow, and only an excuse word directly before the
    name counts ("Lost in thought, Vey turns" is Vey on the page).
    """
    before, after = sentence[:start], sentence[end:]
    if SPEAKER_RE.match(after):
        return None  # 'A shout from Vey: "..."' is Vey speaking
    owned = re.match(r"['’][sS]\s+([A-Za-z]+)(?:\s+([A-Za-z]+))?", after)  # "Vey's letter", "Vey's old letter"
    if owned and (owned.group(1).lower() in ARTIFACTS or (owned.group(2) or "").lower() in ARTIFACTS):
        return "possessive"  # "Velathra's plans", not "Vey's eyes" or "Vey's taken"
    adjacent = before.rsplit(None, 2)[-2:] if before[-1:].isspace() else []
    prev = adjacent[-1].lower() if adjacent and re.fullmatch(r"[\w'’]+", adjacent[-1]) else ""
    prev2 = adjacent[-2].lower() if prev and len(adjacent) > 1 and re.fullmatch(r"[\w'’]+", adjacent[-2]) else ""
    titled = not name_tokens(sentence[start:end])  # "the Captain": the article is part of the name
    if prev in ARTICLES and not titled and re.match(FAMILY, after):
        return "attributive"  # "the Vey brothers"; not "the Warden, who nods"
    if (prev in MENTION_PREPS or (prev in ("of", "about") and prev2 in OF_MENTION)
            or (prev == "from" and prev2 in FROM_MENTION)):
        return "mention"  # "thinks of Vey", "talks about Vey", "a letter from Vey", "whether Vey..."
    subject = len(adjacent) > 1 and (prev2 in PRONOUNS or bool(tokens_re.fullmatch(adjacent[-2])))
    if (prev in REPORT and subject) or (prev == "that" and prev2 in REPORT | SPEECH_WORDS):
        return "reported"  # "Liss knows Vey...", not "Deep in thought Vey..."
    if prev in ("says", "said") and prev2 and (prev2 in PRONOUNS or tokens_re.fullmatch(adjacent[-2])):
        return "reported"  # "Liss says Vey must be at the mill"; not '"Hang it," said Vey'
    gone = GONE_RE.match(after)
    if gone and not ARRIVAL_RE.search(after) and not re.search(r"\b(?:and|now|then)\b", after[gone.end():]):
        return "absent"  # "Bren is away", "Vey has gone to the mill"; not "...and now comes in"
    if LATER_RE.match(after) and not ARRIVAL_RE.search(after):
        return "absent"  # "Vey will be back by noon", "where Vey used to sit"
    return None


def _carried(after: str) -> bool:
    """The absent character sends word and nothing else: "Vey sends word: the hinges are ready." """
    m = re.match(r"\s+(?:has\s+)?(?:sends?|sent)\s+(?:\w+\s+){0,4}?(?:word|news|note|message|letter)\b", after, re.I)
    if m:
        rest = after[m.end():]
        return bool(re.match(r"\s*(?:$|[.:;—–]|that\b|saying\b|to\s+say\b|by\b"
                             r"|(?:to|from)\s+(?:the\s+)?\w+(?:\s+\w+)?\s*(?:$|[.:;]))", rest))
    return bool(re.match(r"\s+(?:has\s+)?(?:sends?|sent)\s+for\s+\w+\s*(?:$|[.:;])", after, re.I))


def _tokens_regex(cast: list[str]):
    variants = set()
    for name in cast:
        full = " ".join(re.sub(r"\([^()]*\)", " ", name).replace("_", " ").split())
        if full and full == full.lower():  # a file stem like "ildric-marr"
            full = " ".join(w[:1].upper() + w[1:] for w in re.split(r"[-\s]+", full) if w)
        if len(name_tokens(full)) > 1 and " " in full:
            variants.update({full, full.upper()})
        titled = re.match(r"(?i)(the|a|an)\s+(.*)", full)
        if titled:  # "The Frost Keeper" is a title: its words are not names
            variants.update({full, full.upper(), f"the {titled.group(2)}", titled.group(2)})
            continue
        if not name_tokens(full) and re.fullmatch(r"[A-Z][a-z]+", full):
            variants.update({full, full.upper()})  # a title-only name: "Captain", "Mother"
        for raw in TOKEN_RE.findall(full):
            token = raw.strip("'’-")
            if token[:1].isupper() and token.rstrip(".") not in TITLES and len(token) >= 2:
                variants.add(token)
                if len(token) >= 3:
                    variants.add(token.upper())
    if not variants:
        return None
    return re.compile(r"\b(?:%s)\b" % "|".join(sorted(map(re.escape, variants), key=len, reverse=True)))


def _anchor(draft: str, findings: list) -> dict | None:
    lines = [line.strip() for line in draft.splitlines() if line.strip()]
    anchors = [line for line in lines if ANCHOR_LINE_RE.search(line)]
    if not anchors:
        findings.append({"check": "anchor_missing", "fix": "edit",
                         "detail": "The draft does not close with a Scene: | Location: | Present: line."})
        return None
    if len(anchors) > 1:
        findings.append({"check": "anchor_duplicated", "fix": "edit",
                         "detail": f"The draft carries {len(anchors)} anchor lines; keep one, last."})
    if not ANCHOR_LINE_RE.search(lines[-1]):
        findings.append({"check": "anchor_not_last", "fix": "edit",
                         "detail": "Text follows the anchor line; the anchor closes the draft."})
    fields = {}
    for segment in anchors[-1].strip("*` ").split("|"):
        key, _, value = segment.partition(":")
        fields[key.strip(" *`").lower()] = value.strip(" *`")
    if not fields.get("location") or not fields.get("present"):
        findings.append({"check": "anchor_malformed", "fix": "edit",
                         "detail": "The anchor needs Scene, Location and Present fields."})
    return fields


def _norm_time(value) -> str | None:
    m = TIME_RE.search(str(value or ""))
    return f"{int(m.group(1)):02d}:{m.group(2)}" if m else None


MAX_DAY_JUMP = 7  # the logs' longest jump between beats is 4 days


ANCHOR_DAY_RE = re.compile(r"\s*Day\s+(\d+)(?:\s*(?:→|->|–)\s*(\d+)(?![\d:]))?"
                           r"(?:[^;|]{0,40}?(?:→|->|–|\bto\b)\s*Day\s+(\d+))?")


def _day_findings(scene_field: str, delta: dict, scene_day, file_day=None) -> list[dict]:
    """The delta's day picks the beat's day file, and a rotation cannot be undone: hold it to the anchor.

    The anchor's day is the day its leading span ends on: "Scene: Day 196, ~22:00 to Day 197,
    ~05:50" is Day 197. A clock later in the field ("; THE CURE Day 3 of 30") is not the day, and
    "the siege, Day 3 of it" names none.
    """
    m = ANCHOR_DAY_RE.match(scene_field)
    anchor_day = int(m.group(3) or m.group(2) or m.group(1)) if m else None
    stated = delta.get("currentDate")
    delta_day = fiction_day(stated) if isinstance(stated, str) else None
    findings = []
    if anchor_day is not None:
        if "currentDate" in delta and delta_day != anchor_day:
            findings.append({"check": "delta_day_mismatch", "fix": "edit",
                             "detail": f"currentDate in the delta is not the anchor's Day {anchor_day} "
                                       f'(write it as "Day {anchor_day} (<weekday>)").'})
        elif "currentDate" not in delta and scene_day not in (None, anchor_day):
            findings.append({"check": "delta_day_missing", "fix": "edit",
                             "detail": f"The anchor moves the day to Day {anchor_day} but the delta has no currentDate."})
    elif delta_day is not None and delta_day != scene_day:
        findings.append({"check": "delta_day_unanchored", "fix": "review",
                         "detail": f"The delta moves the day to Day {delta_day} and the anchor names no day to confirm it."})
    new_day = delta_day if delta_day is not None else anchor_day
    base = max((d for d in (scene_day, file_day) if d is not None), default=None)  # a flashback lowers the first
    if new_day is not None and base is not None and new_day - base > MAX_DAY_JUMP:
        findings.append({"check": "delta_day_jump", "fix": "review",
                         "detail": f"The beat moves the day from Day {base} to Day {new_day}; the log would "
                                   "rotate past every day between, and a rotation cannot be undone."})
    return findings


NO_DELTA = ("none", "null", "{}", "-", "n/a", "no change", "no changes", "unchanged")


def parse_delta(text: str):
    """(delta, parsed): SCENE_STATE_DELTA as flat dot-path keys; parsed is False for a non-mapping."""
    text = (text or "").strip()
    delta = {}
    if not text or text.lower().strip(" .()") in NO_DELTA:
        return delta, True
    data = parse_yaml(text)
    if not isinstance(data, dict):
        return delta, False
    for key, value in data.items():
        if key == "scene" and isinstance(value, dict):
            delta.update({f"scene.{k}": v for k, v in value.items()})
        else:
            delta[key] = value
    return delta, True


def lint(turn: dict, scene: dict, config: dict, project) -> dict:
    """Deterministic checks on one GM turn. Findings carry fix: edit or review."""
    started = time.perf_counter()
    project = Path(project)
    findings, events = [], []
    draft = turn.get("DRAFT", "")
    fields = _anchor(draft, findings) if draft.strip() else None
    if not draft.strip():
        findings.append({"check": "draft_empty", "fix": "edit", "detail": "The GM returned no draft."})
    present = present_names(fields.get("present", "")) if fields else []
    pc = scene.get("pc")
    prior = [n for n in scene["participants"] if not (pc and same_person(n, pc))]
    present_npcs = [n for n in present if not (pc and same_person(n, pc))]

    # Named actors against the people on the page. A newcomer the anchor lists
    # has arrived; anyone else must arrive on the page or send word.
    from_start = list(scene["participants"]) + ([pc] if pc else [])
    newcomers = [n for n in present if not any(same_person(n, p) for p in from_start)]
    body = "\n".join(line[:ANCHOR_START_RE.search(line).start()] if ANCHOR_LINE_RE.search(line) else line
                     for line in draft.splitlines())
    narration = QUOTE_RE.sub(_quote, body.replace("*", "").replace("_", ""))
    tokens_re = _tokens_regex(_cast(project, config, from_start + present))
    hard = set()  # names a common word cannot excuse: the scene's people and turnGate.cast
    for name in from_start + present + list(config["cast"]):
        hard |= name_tokens(name) | {_plain(name)}
    done = set()
    lowercase = set(re.findall(r"\b[a-z][\w'’-]*", narration))
    sentences = [s for s in SENTENCE_RE.split(narration) if s.strip()] if tokens_re else []
    for index, sentence in enumerate(sentences):
        after_quote = index > 0 and sentences[index - 1].rstrip().endswith("«Q».")
        for m in tokens_re.finditer(sentence):
            name = m.group(0)
            if name.lower() in done or any(same_person(name, p) for p in from_start):
                continue
            if (not sentence[:m.start()].strip() and not after_quote and not name.isupper()
                    and name.lower() in lowercase and name.lower() not in hard):
                continue  # "Frost runs along the wall" where the draft also says "frost"
            if _excused(sentence, m.start(), m.end(), tokens_re):
                continue
            canonical = name[:1] + name[1:].lower() if name.isupper() else name
            if ARRIVAL_RE.search(sentence[m.start():]):
                done.add(name.lower())
                from_start.append(canonical)
                events.append({"kind": "arrival", "name": canonical})
            elif any(same_person(name, n) for n in newcomers):
                done.add(name.lower())
            elif _carried(sentence[m.end():]):
                events.append({"kind": "carried", "name": canonical})  # a later mention is still checked
            else:
                done.add(name.lower())
                findings.append({"check": "presence", "fix": "review", "name": canonical,
                                 "detail": f"{canonical} is on the page (speaks, acts or is addressed) but is not "
                                           "in the scene, does not arrive and sends no word."})

    # SCENE_STATE_DELTA: known keys, and agreement with the anchor.
    delta, parsed = parse_delta(turn.get("SCENE_STATE_DELTA", ""))
    if not parsed:
        findings.append({"check": "delta_malformed", "fix": "edit",
                         "detail": "SCENE_STATE_DELTA is not a key: value mapping."})
    known = DELTA_KEYS | set(config["deltaKeys"])
    mirrors = sorted(k for k in delta if k in ROOT_MIRRORS)
    unknown = sorted(k for k in delta if k not in known and k not in ROOT_MIRRORS)
    if mirrors:
        findings.append({"check": "delta_root_mirror", "fix": "edit",
                         "detail": f"Root mirror keys {', '.join(mirrors)}: emit scene.* keys; the command syncs mirrors."})
    if unknown:
        findings.append({"check": "delta_unknown_key", "fix": "edit",
                         "detail": f"Unknown delta keys {', '.join(unknown)}."})
    if fields:
        if "scene.participants" in delta and not isinstance(delta["scene.participants"], list):
            findings.append({"check": "delta_malformed", "fix": "edit",
                             "detail": "scene.participants in the delta must be a list."})
        elif "scene.participants" in delta:
            listed = [n for n in _participant_names(delta["scene.participants"]) if not (pc and same_person(n, pc))]
            if not same_people(listed, present_npcs):
                findings.append({"check": "delta_participants_mismatch", "fix": "edit",
                                 "detail": "scene.participants in the delta disagrees with the anchor's Present list."})
        elif not same_people(prior, present_npcs):
            findings.append({"check": "delta_participants_missing", "fix": "edit",
                             "detail": "The anchor's Present list changed but the delta has no scene.participants."})
        anchor_time = _norm_time(fields.get("scene"))
        if anchor_time:
            if "scene.time" in delta:
                if _norm_time(delta["scene.time"]) not in (None, anchor_time):
                    findings.append({"check": "delta_time_mismatch", "fix": "edit",
                                     "detail": f"scene.time in the delta disagrees with the anchor's ~{anchor_time}."})
            elif _norm_time(scene.get("time")) not in (None, anchor_time):
                findings.append({"check": "delta_time_missing", "fix": "edit",
                                 "detail": f"The anchor moves the clock to ~{anchor_time} but the delta has no scene.time."})
        findings += _day_findings(fields.get("scene") or "", delta, scene.get("day"), scene.get("log_day"))
    return {
        "findings": findings,
        "events": events,
        "present": present,
        "anchored": bool(fields),
        "prior": scene["participants"],
        "delta": delta,
        "narration": narration,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
    }


# ---------------------------------------------------------------- routing


def _declared(text: str, key: str) -> bool:
    """key: true, also "key": "true" or 'true'."""
    return bool(re.search(r"""\b%s["']?\s*:\s*["']?(?:true|yes)\b""" % key, text, re.I))


def _priced_regex():
    try:
        rule = json.loads(PRICED_RULE.read_text(encoding="utf-8"))
        return re.compile(rule["match_regex"], re.I if rule.get("match_ci", True) else 0)
    except (OSError, ValueError, KeyError, re.error):
        return None


def beat_triggers(turn: dict, result: dict, scene: dict, config: dict) -> list[str]:
    """What makes a beat consequential, each derived without a model call."""
    keeper, source = turn.get("KEEPER_INPUT", ""), turn.get("SOURCE_LOG", "")
    triggers = []
    if re.search(r"\[validate\]", keeper, re.I):
        triggers.append("validate_tag")
    priced = _priced_regex()
    if _declared(source, "priced_stake_touched") or (priced and keeper and priced.search(keeper)):
        triggers.append("priced_stake")
    if _declared(source, "canon_proposed"):
        triggers.append("canon_proposed")
    pc, delta = scene.get("pc"), result["delta"]
    listed = delta.get("scene.participants")
    pc_entry = next((p for p in (listed if isinstance(listed, list) else [])
                     if isinstance(p, dict) and pc and same_person(str(p.get("name", "")), pc)), None)
    if _declared(source, "pc_changed") or (pc_entry and pc_entry.get("role") and pc_entry["role"] != scene.get("pc_role")):
        triggers.append("pc_change")
    prior = [n for n in scene["participants"] if not (pc and same_person(n, pc))]
    present = [n for n in result["present"] if not (pc and same_person(n, pc))]
    if (result["anchored"] and not same_people(prior, present)) or any(e["kind"] == "arrival" for e in result["events"]):
        triggers.append("arrival_departure")
    if FIGHT_RE.search(result["narration"]):
        triggers.append("fight_or_capture")
    if any(pattern.search(turn.get("DRAFT", "")) for pattern in config["patterns"]):
        triggers.append("pattern")
    return triggers


def route(result: dict, triggers: list[str], config: dict, recheck: bool = False, final: bool = False,
          rewrite: bool = False) -> dict:
    """ship, fix (mechanical, no model) or review.

    final follows a review: ship or fix only. rewrite answers a confirmed
    violation, so it is always reviewed, as is a [validate] beat.
    """
    findings = result["findings"]
    review_findings = [f for f in findings if f["fix"] == "review"]
    edit_findings = [f for f in findings if f["fix"] == "edit"]
    beat = "consequential" if triggers else "routine"
    policy = config["review"]
    if final:
        step = "fix" if edit_findings else "ship"
    elif edit_findings and not recheck:
        step = "fix"  # mechanical: the edit pass corrects it, then check --recheck
    elif rewrite or "validate_tag" in triggers:
        step = "review"
    elif policy == "lint-only":
        step = "ship"
    elif policy == "every" or triggers or review_findings or edit_findings:
        step = "review"
    else:
        step = "ship"
    return {
        "next": step,
        "beat": beat,
        "policy": policy,
        "reasons": triggers + [f"lint:{f['check']}" for f in findings],
        "max_rewrites": config["rewrites"][beat],
        "gm_model": config["models"]["gm"],
        "reviewer_model": config["models"]["reviewer"],
        "adjudicator_model": config["models"]["adjudicator"],
    }


# ----------------------------------------------------------------- config


def _top_level_block(text: str, key: str) -> str:
    lines, inside = [], False
    for line in text.splitlines():
        if re.match(r"^%s\s*:" % re.escape(key), line):  # block form, or a flow mapping on the line
            inside = True
        elif inside and line[:1] not in ("", " ", "\t", "#"):
            break
        if inside:
            lines.append(line)
    return "\n".join(lines)


def _mode_block(text: str, key: str, warnings: list) -> dict:
    block = _top_level_block(text, key)
    parsed = parse_yaml(block) if block else None
    value = parsed.get(key) if isinstance(parsed, dict) else None
    if value is not None and not isinstance(value, dict):
        warnings.append(f"{key} is not a mapping; using defaults.")
        value = None
    return value or {}


def load_config(project, path=None) -> dict:
    """The turnGate and sessionLog blocks of the project's rp mode file, over the defaults."""
    config = copy.deepcopy(DEFAULTS)
    warnings = []
    mode = Path(path) if path else Path(project) / MODE_FILE
    text = mode.read_text(encoding="utf-8", errors="replace") if mode.is_file() else ""
    block = _mode_block(text, "turnGate", warnings)
    log_block = _mode_block(text, "sessionLog", warnings)
    config["log"] = dict(LOG_DEFAULTS)
    if "rotation" in log_block:
        value = {False: "off", True: "day"}.get(log_block["rotation"], log_block["rotation"])
        if value in ROTATIONS:
            config["log"]["rotation"] = value
        else:
            warnings.append(f"sessionLog.rotation {log_block['rotation']!r} is not one of {', '.join(ROTATIONS)}.")
    if "previousBeats" in log_block:
        value = log_block["previousBeats"]
        if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= MAX_PREVIOUS_BEATS:
            config["log"]["previousBeats"] = value
        else:
            warnings.append(f"sessionLog.previousBeats {value!r} must be 0-{MAX_PREVIOUS_BEATS}.")
    if "review" in block:
        if block["review"] in POLICIES:
            config["review"] = block["review"]
        else:
            warnings.append(f"turnGate.review {block['review']!r} is not one of {', '.join(POLICIES)}.")
    for name in ("models", "rewrites"):
        if name in block and not isinstance(block[name], dict):
            warnings.append(f"turnGate.{name} must be a mapping; using defaults.")
            block[name] = {}
    for key, value in (block.get("models") or {}).items():
        if key in config["models"]:
            if value in MODELS:
                config["models"][key] = value
            else:
                warnings.append(f"turnGate.models.{key} {value!r} is not one of {', '.join(MODELS)}.")
    for key, value in (block.get("rewrites") or {}).items():
        if key in config["rewrites"]:
            if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= MAX_REWRITES:
                config["rewrites"][key] = value
            else:
                warnings.append(f"turnGate.rewrites.{key} {value!r} must be 0-{MAX_REWRITES}.")
    for key in ("cast", "deltaKeys", "consequentialPatterns"):
        value = block.get(key)
        if value is None:
            continue
        if isinstance(value, list) and all(isinstance(v, str) for v in value):
            config[key] = value
        else:
            warnings.append(f"turnGate.{key} must be a list of strings.")
    config["patterns"] = []
    for pattern in config["consequentialPatterns"]:
        try:
            config["patterns"].append(re.compile(pattern, re.I))
        except re.error as exc:
            warnings.append(f"turnGate.consequentialPatterns {pattern!r}: {exc}.")
    config["warnings"] = warnings
    return config


# -------------------------------------------------------------------- log


def append_log(project, record: dict) -> dict:
    line = {"date": datetime.datetime.now().astimezone().isoformat(timespec="seconds"), "gate": "rp_gate"}
    line.update({k: v for k, v in record.items() if v is not None})
    path = Path(project) / VALIDATOR_LOG
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(line, ensure_ascii=False) + "\n")
    return line


def _csv(value):
    return [part.strip() for part in value.split(",") if part.strip()] if value else None


# ------------------------------------------------------- fiction-day files
# One log file per fiction day, under the session's log_dir: day-0196.md. A beat
# belongs to the day it ends on (the logs write the next day's Day Plan before
# the night beat that reaches it). Rotation runs before a beat ships, in this
# order: publish the new file whole, swap the marker atomically, then footer the
# old file. Each step is safe to re-run; a closed file is never edited again.

FICTION_DAY_RE = re.compile(r"\bDay\s+(\d+)(?:\s*(?:→|->|–)\s*(\d+)(?![\d:]))?")  # "Day 163→164" is 164
BEAT_HEADING_RE = re.compile(r"^#{1,6}\s+(?:Beat\s+|B(?=\d))(\d+[A-Za-z]?)(?![\w-])(.*)$")
BEAT_SEPARATORS = (":", "—", "–", "-")
DAY_PLAN_RE = re.compile(r"(?i)^#{1,6}\s+day plan\b")
ROTATED_RE = re.compile(r"<!-- rp-rotated-to: (.+?) -->")  # a path may hold spaces ("Work/rp/My Run/log")
DAY_BLOCK_START = "<!-- rp-day-file"
DAY_BLOCK_END = "<!-- /rp-day-file -->"
CONTINUES_RE = re.compile(r"\bContinues\s+`([^`\n]+?\.md)`")
HEADER_COPY_LIMIT = 2000


def fiction_day(text) -> int | None:
    """The fiction day a text ends on: "Day 196 (Monday)" is 196; "Day 195 ... to Day 196" is 196."""
    days = [int(m.group(2) or m.group(1)) for m in FICTION_DAY_RE.finditer(str(text or ""))]
    return days[-1] if days else None


def parse_beat_heading(line: str):
    """{"beat", "day"} for a beat heading, else None.

    "### Beat 4123: Day 195, ~19:05 to Day 196, ~05:50 (Sunday). (...)" is Beat
    4123 on Day 196: the day comes from the time span, before the parentheses.
    "Beat 4100 PRE-COMMIT", SPEC, RULING and AMENDMENT headings are the next
    beat's working, not beats; "Beat 3086 (INSERT, ...)" is a beat with no day.
    """
    m = BEAT_HEADING_RE.match(line)
    if not m:
        return None
    rest = m.group(2).strip()
    if rest.startswith("("):  # "(REVISED 2, ...): Day 136", "(re-run): Day 180", "(INSERT ...)."
        close = rest.find(")")
        rest = rest[close + 1:].strip() if close >= 0 else ""
        if not rest.startswith(BEAT_SEPARATORS):
            return {"beat": m.group(1), "day": None}
    elif not rest.startswith(BEAT_SEPARATORS):
        return None
    return {"beat": m.group(1), "day": fiction_day(rest[1:].split("(", 1)[0])}


def _goes_forward(line: str, behind=None) -> bool:
    """A heading that belongs to the next beat: its Day Plan or its working. Others stay behind,
    as does a working of the beat before ("Beat 3705 AMENDMENT" after Beat 3705)."""
    m = BEAT_HEADING_RE.match(line)
    return bool(DAY_PLAN_RE.match(line) or (m and m.group(1) != behind))


def _beat_units(lines: list[str], start: int = 0, end: int | None = None) -> list[dict]:
    """The beats in lines[start:end], each with the lines that belong to it.

    A unit runs from its first forward heading (a Day Plan, the beat's PRE-COMMIT)
    or its own heading, through its prose and any trailing PAUSE or END block, to
    the next unit. Lines are 1-based; start and end skip blank lines; cut is the
    0-based first line, untrimmed.
    """
    end = len(lines) if end is None else end
    fenced = _fenced(lines)
    heads = [i for i in range(start, end) if i not in fenced and HEADING_RE.match(lines[i])]
    units, previous_block, h, behind = [], start, 0, None
    for n, i in enumerate(heads):
        info = parse_beat_heading(lines[i])
        if info is None:
            continue
        while h < n and heads[h] < previous_block:
            h += 1
        cut = next((heads[k] for k in range(h, n) if _goes_forward(lines[heads[k]], behind)), i)
        behind = info["beat"]
        units.append({"beat": info["beat"], "day": info["day"], "heading": i + 1, "cut": cut})
        previous_block = heads[n + 1] if n + 1 < len(heads) else end
    for n, unit in enumerate(units):
        stop = units[n + 1]["cut"] if n + 1 < len(units) else end
        first = unit["cut"]
        while first < stop and not lines[first].strip():
            first += 1
        last = stop
        while last > first and not lines[last - 1].strip():
            last -= 1
        unit["start"], unit["end"] = first + 1, last
    return units


def _read_lines(path: Path):
    try:
        return path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    except OSError:
        return None


def _frontmatter_end(lines: list[str]):
    """Index of the closing --- of the leading frontmatter, or None."""
    if not lines or lines[0].strip() != "---":
        return None
    return next((i for i in range(1, min(len(lines), 2000)) if lines[i].strip() == "---"), None)


def _body_start(lines: list[str]) -> int:
    """Index of the first line after the header: the first Day Plan or beat heading, or the end."""
    fm = _frontmatter_end(lines)
    start = fm + 1 if fm is not None else 0
    return next((i for i in range(start, len(lines)) if HEADER_END_RE.match(lines[i])), len(lines))


def _footer_start(lines: list[str]) -> int:
    """Index where a rotation footer begins (its --- rule), or the end of the file."""
    found = _final_footer(lines)
    if found is None:
        return len(lines)
    marker = found[0]
    while marker > 0 and (not lines[marker - 1].strip() or lines[marker - 1].strip() == "---"):
        marker -= 1
    return marker


def _final_footer(lines: list[str]):
    """(index, target) of the footer that ends the file, else None. Only the final block counts: a
    header quoting a closed file's tail ("Resume point, verbatim") must not close an open file."""
    i = len(lines) - 1
    while i >= 0 and (not lines[i].strip() or lines[i].startswith("**Continued in `")):
        i -= 1
    m = ROTATED_RE.fullmatch(lines[i].strip()) if i >= 0 else None
    return (i, m.group(1)) if m else None


def rotation_footer(path) -> str | None:
    """The file a closed log continues in, from the footer that ends it; None while it is open."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - 8192))
            tail = fh.read().decode("utf-8", "replace").splitlines()[1:] or [""]
    except OSError:
        return None
    found = _final_footer(tail)
    return found[1] if found else None


def _follow_rotation(project: Path, path: Path):
    """Follow rotation footers forward to the open file: (path, hops). A loop stops it."""
    seen = {path}
    while True:
        nxt = rotation_footer(path)
        target = _inside_sessions(project, nxt) if nxt else None
        if target is None or not target.is_file() or target in seen:
            return path, len(seen) - 1
        seen.add(target)
        path = target


def _continues_target(project: Path, data: dict):
    """The session file a day file's frontmatter continues, if it exists under the session directory."""
    value = data.get("continues")
    target = _inside_sessions(project, value) if isinstance(value, str) and value.strip() else None
    return _relative(project, target) if target and target.is_file() else None


def _previous_file(project: Path, data: dict, lines: list[str]):
    """The file before this one: frontmatter continues:, else the header's "Continues `<file>`" line."""
    named = _continues_target(project, data)
    if named or data.get("log_day") is not None:
        return named
    for line in lines[:_body_start(lines)]:
        m = CONTINUES_RE.search(line)
        target = _inside_sessions(project, m.group(1)) if m else None
        if target and target.is_file():
            return _relative(project, target)
    return None


def _log_day(data: dict) -> int | None:
    value = data.get("log_day")
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return int(value) if isinstance(value, str) and value.strip().isdigit() else None


def _days(data: dict, lines: list[str] | None = None):
    """(file_day, state_day). A day file's day is its log_day; a legacy log's, the highest day its
    beats reached (its frontmatter only when no beat names a day). state_day, the day a beat without
    a day of its own is on, is the later of that and the frontmatter's: a flashback's delta lowers
    the frontmatter but not the log, an accepted surrender raises it before any beat.
    A log's frontmatter can also go stale: the 10-17 log's says Day 176, its beats end on Day 195.
    """
    stated = fiction_day(data.get("currentDate")) if isinstance(data.get("currentDate"), str) else None
    file_day = _log_day(data)
    if file_day is None:
        file_day = max((info["day"] for line in lines or [] if (info := parse_beat_heading(line))
                        and info["day"] is not None), default=stated)
    known = [d for d in (file_day, stated) if d is not None]
    return file_day, max(known) if known else None


def _file_day(data: dict, lines: list[str] | None = None) -> int | None:
    return _days(data, lines)[0]


def _span(rel: str, units: list[dict], beats: int) -> dict:
    chosen = units[-beats:]
    return {"path": rel, "start": chosen[0]["start"], "end": chosen[-1]["end"], "beats": len(chosen),
            "first_beat": chosen[0]["beat"], "last_beat": chosen[-1]["beat"]}


def _range(rel: str, lines: list[str], start: int, stop: int):
    first, last = start, stop
    while first < last and not lines[first].strip():
        first += 1
    while last > first and not lines[last - 1].strip():
        last -= 1
    return {"path": rel, "start": first + 1, "end": last} if last > first else None


def _last_beats(project: Path, rel, beats: int):
    """The last beats of a file, its footer excluded; any END block after them included."""
    lines = _read_lines(project / rel) if rel and beats else None
    if not lines:
        return None
    stop = _footer_start(lines)
    units = _beat_units(lines, _body_start(lines), stop)
    if not units:
        return None
    span = _span(rel, units, beats)
    span["end"] = (_range(rel, lines, units[-1]["cut"], stop) or {"end": span["end"]})["end"]
    return span


def _outside(plans: list[dict], current) -> list[dict]:
    """The plans the current range does not already hold."""
    if not current:
        return plans
    return [r for r in plans if r["path"] != current["path"] or r["end"] < current["start"] or r["start"] > current["end"]]


MAX_READ_HOPS = 64  # files walked back for the read; a run of beatless day files cannot loop it


def _day_plans(rel: str, lines: list[str], lo: int, start: int, stop: int, day: int) -> list[dict]:
    """Every Day Plan section for `day` in lines[lo:stop], in order: from lo, a heading that names the day (a plan
    written early, before an earlier day's last beat); from start, the day's own region, a heading that names no day
    too (an ADDENDUM, a second session's plan). Each runs through its subheadings to the next beat, beat working or
    Day Plan heading; a fenced heading neither opens nor ends one."""
    fenced, found = _fenced(lines), []
    for i in range(lo, stop):
        if i in fenced or not DAY_PLAN_RE.match(lines[i]):
            continue
        named = {int(d) for m in FICTION_DAY_RE.finditer(lines[i]) for d in m.groups() if d}
        if day not in named and (named or i < start):
            continue
        end = next((j for j in range(i + 1, stop) if j not in fenced
                    and (DAY_PLAN_RE.match(lines[j]) or BEAT_HEADING_RE.match(lines[j]))), stop)
        section = _range(rel, lines, i, end)
        if section:
            section["heading"] = lines[i].strip()[:120]
            found.append(section)
    return found


def continuity(project, session_file, beats: int = LOG_DEFAULTS["previousBeats"]):
    """What the GM and the gate read each turn: the last `beats` beats, wherever they fall.

    current is the current fiction day's part: its last `beats` beats, or the
    whole day so far (its Day Plan included) when it holds fewer. previous, oldest
    first, tops the read up to `beats` from before the day: the same legacy log's
    earlier beats, then the files before it, as far back as it takes. A day file
    is one day; a legacy log holds many, and its day starts with the day's first unit.
    """
    project = Path(project)
    rel = _relative(project, project / session_file)
    lines = _read_lines(project / rel)
    data = read_frontmatter(project / rel)
    file_day, state_day = _days(data, lines)
    day = file_day if data.get("log_day") is not None else state_day
    if lines is None or day is None:
        return None
    body, stop = _body_start(lines), _footer_start(lines)
    units = _beat_units(lines, body, stop)
    start, k = body, 0
    if data.get("log_day") is None:  # a legacy log: the day starts where the beats first reach it
        top, k = None, len(units)
        for n, unit in enumerate(units):
            top = unit["day"] if top is None else max(top, unit["day"] or top)
            if top is not None and top >= day:
                k = n
                break
        if k < len(units):
            start = units[k]["cut"]
        elif units:  # no beat of the day yet: it starts at the first forward heading after the last beat
            start = next((i for i in range(units[-1]["heading"], stop) if _goes_forward(lines[i])), stop)
    today, earlier = units[k:], units[:k]
    plans = _day_plans(rel, lines, body, start, stop, day)
    if not plans:  # the day's plan written early in the file before, before that file's last beat
        back = _previous_file(project, data, lines)
        back_lines = _read_lines(project / back) if back else None
        if back_lines:
            back_stop = _footer_start(back_lines)
            plans = _day_plans(back, back_lines, _body_start(back_lines), back_stop, back_stop, day)
    result = {"day": day, "beats": beats, "current": None, "previous": [], "plans": plans}
    if beats <= 0:
        return result
    if len(today) >= beats:  # never the whole day: its last beats only
        current = _range(rel, lines, today[-beats]["cut"], stop)
        if current:
            current.update(beats=beats, whole_day=False, first_beat=today[-beats]["beat"], last_beat=today[-1]["beat"])
        result["current"] = current
        result["plans"] = _outside(plans, current)
        return result
    current = _range(rel, lines, start, stop)
    if current:
        current.update(beats=len(today), whole_day=True, first_beat=today[0]["beat"] if today else None,
                       last_beat=today[-1]["beat"] if today else None)
    result["current"] = current
    result["plans"] = _outside(plans, current)
    need, previous = beats - len(today), []
    if earlier:  # the same log's beats before the day
        span = _span(rel, earlier, need)
        before = start
        while before > 0 and not lines[before - 1].strip():
            before -= 1
        span["end"] = min(span["end"], before)
        previous.append(span)
        need -= span["beats"]
    seen, here = {rel}, (data, lines)
    for _hop in range(MAX_READ_HOPS):
        if need <= 0:
            break
        back = _previous_file(project, *here)
        if back is None or back in seen:
            break
        seen.add(back)
        span = _last_beats(project, back, need)
        if span:
            previous.append(span)
            need -= span["beats"]
        here = (read_frontmatter(project / back), _read_lines(project / back) or [])
    result["previous"] = list(reversed(previous))
    return result


def session_files(project, current: str, opened) -> list[str]:
    """The files one session spans, oldest first: from the file it opened in, through rotations.

    A marker without session_opened was written by the old /rp:start, which only ever opened a
    legacy log: the session then reaches back to the nearest legacy log in the chain.
    """
    project = Path(project)
    if not isinstance(opened, str) or not opened.strip():
        chain = [current]
        while read_frontmatter(project / chain[-1]).get("log_day") is not None and len(chain) < 10000:
            previous = _continues_target(project, read_frontmatter(project / chain[-1]))
            if previous is None or previous in chain:
                return [current]
            chain.append(previous)
        return list(reversed(chain))
    opened = _relative(project, project / opened)
    chain = [current]
    while chain[-1] != opened and len(chain) < 500:
        previous = _continues_target(project, read_frontmatter(project / chain[-1]))
        if previous is None or previous in chain:
            return [current]
        chain.append(previous)
    return list(reversed(chain)) if chain[-1] == opened else [current]


def _interrupted(project: Path, rel: str):
    """The file a rotation into rel left without its footer, if any."""
    data = read_frontmatter(project / rel)
    previous = _continues_target(project, data) if data.get("log_day") is not None else None
    return previous if previous and not rotation_footer(project / previous) else None


def _log_dir(project: Path, data: dict):
    """(log_dir, warning): the session's day-file directory, kept under the session directory."""
    value = data.get("log_dir")
    if not isinstance(value, str) or not value.strip():
        return None, (f'The session frontmatter names no log_dir (log_dir: "{SESSION_DIR}/<campaign>/log"; '
                      "/rp:start writes it), so the log does not rotate.")
    lexical = os.path.normpath(os.path.join(project, value.strip()))
    resolved, sessions = Path(lexical).resolve(), (project / SESSION_DIR).resolve()
    if sessions not in resolved.parents:  # symlinks resolved, as for every session path
        return None, f"log_dir {value!r} is not under {SESSION_DIR}/; the log does not rotate."
    return Path(os.path.relpath(lexical, project)).as_posix(), None


def _yaml_value(value) -> str:
    return str(value) if isinstance(value, int) else json.dumps(value, ensure_ascii=False)


def _upsert_frontmatter(lines: list[str], updates: dict) -> list[str]:
    """Set or drop top-level scalar keys in raw frontmatter lines; every other line is kept as written."""
    body = lines[1:-1]
    for key, value in updates.items():
        index = next((i for i, line in enumerate(body) if re.match(r"%s\s*:" % re.escape(key), line)), None)
        new = [] if value is None else [f"{key}: {_yaml_value(value)}\n"]
        if index is None:
            body += new
            continue
        stop = index + 1
        while stop < len(body) and (body[stop][:1] in (" ", "\t") or body[stop].startswith("- ")):
            stop += 1
        body[index:stop] = new
    return [lines[0]] + body + [lines[-1]]


def _inherited_header(lines: list[str], start: int) -> list[str]:
    """The header as written, minus the old file's generated block (or a legacy log's title)."""
    end = start
    while end < len(lines) and not HEADER_END_RE.match(lines[end]) and not ROTATED_RE.search(lines[end]):
        end += 1
        if end - start > HEADER_COPY_LIMIT:
            raise ValueError(f"the header runs past {HEADER_COPY_LIMIT} lines without a Day Plan or beat heading")
    region = [line if line.endswith("\n") else line + "\n" for line in lines[start:end]
              if not line.startswith(OPENED_MARK)]  # a session's opening belongs to the file it opened in
    opening = next((i for i, line in enumerate(region) if line.startswith(DAY_BLOCK_START)), None)
    closing = next((i for i in range(opening, len(region)) if region[i].strip() == DAY_BLOCK_END), None) \
        if opening is not None else None
    if closing is None and opening is not None:  # the closing comment was lost: end at the Continues line
        closing = next((i for i in range(opening, min(len(region), opening + 8))
                        if region[i].startswith("Continues `")), opening)
    if closing is not None:
        region = region[:opening] + region[closing + 1:]
    else:
        title = next((i for i, line in enumerate(region) if line.strip()), None)
        if title is not None and re.match(r"#\s", region[title]):
            region = region[:title] + region[title + 1:]
    while region and not region[0].strip():
        region.pop(0)
    while region and not region[-1].strip():
        region.pop()
    return region + ["\n"] if region else []


def _day_file_text(path: Path, rel: str, data: dict, day: int, current_date: str, log_dir: str, today: str) -> str:
    lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines(keepends=True)
    fm_end = _frontmatter_end([line.rstrip("\n") for line in lines])
    frontmatter = lines[:fm_end + 1] if fm_end is not None else ["---\n", "---\n"]
    frontmatter = [line if line.endswith("\n") else line + "\n" for line in frontmatter]
    frontmatter = _upsert_frontmatter(frontmatter, {"status": None, "log_dir": log_dir, "log_day": day,
                                                    "continues": rel, "opened": today, "currentDate": current_date})
    header = _inherited_header(lines, fm_end + 1 if fm_end is not None else 0)
    campaign = data.get("campaign")
    title = f"# {campaign}: Day {day}" if isinstance(campaign, str) and campaign.strip() else f"# Day {day}"
    since = _file_day(data)
    block = [f"{DAY_BLOCK_START}: Day {day}; @LINES@ lines as generated; the header below is copied as "
             f"written from {rel} -->\n",
             title + "\n", "\n",
             f"Continues `{rel}`" + (f" (Day {since})" if since is not None else "") + f". Opened {today}.\n",
             DAY_BLOCK_END + "\n", "\n"]
    text = "".join(frontmatter) + "\n" + "".join(block) + "".join(header)
    return text.replace("@LINES@", str(len(text.splitlines())), 1)


def _publish(target: Path, text: str, replace: bool = False) -> None:
    """Write the file whole: a temp file, then a hard link (never over an existing file) or a replace."""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.tmp-{os.getpid()}")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        if replace:
            os.replace(tmp, target)
            return
        try:
            os.link(tmp, target)
        except OSError as exc:
            if exc.errno not in (errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EMLINK):
                raise
            with open(target, "x", encoding="utf-8") as fh:  # no hard links here: exclusive create
                fh.write(text)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def _swap_marker(project: Path, fields: dict) -> None:
    """Replace the marker atomically: it never goes missing, and its other lines are kept."""
    path = project / MARKER
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        lines = []
    out, seen = [], set()
    for line in lines:
        key, _rest = _split_key(line.strip())
        if key in fields:
            if key not in seen:
                out.append(f"{key}: {fields[key]}")
                seen.add(key)
        elif line.strip():
            out.append(line)
    out += [f"{key}: {value}" for key, value in fields.items() if key not in seen]
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _append_footer(path: Path, target: str, day, today: str, beat=None) -> bool:
    """Close a log with one write: the footer naming the file it continues in. Never twice."""
    note = f"Day {day}, rotated {today}" + (f" at Beat {str(beat)[:40]}" if beat else "")  # the tail read is 8 KB
    footer = (f"\n---\n\n<!-- rp-rotated-to: {target} -->\n"
              f"**Continued in `{target}`** ({note}). This file is closed and is never edited again.\n")
    with open(path, "ab") as fh:
        if fcntl:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        if rotation_footer(path):
            return False
        size = fh.seek(0, os.SEEK_END)
        if size:
            with open(path, "rb") as tail:
                tail.seek(size - 1)
                footer = ("" if tail.read(1) == b"\n" else "\n") + footer
        fh.write(footer.encode("utf-8"))
        fh.flush()
        os.fsync(fh.fileno())
    return True


def _line_count(path: Path) -> int:
    return len(_read_lines(path) or [])


def _beat_day(day, turn_text, state_day, heading):
    """(day, source): the beat's fiction day. The gated delta, then the session's day; the heading last."""
    if day is not None:
        return day, "argument"
    delta, _parsed = parse_delta(parse_turn(turn_text).get("SCENE_STATE_DELTA", "")) if turn_text else ({}, True)
    stated = delta.get("currentDate")  # "Day 197 (Tuesday)"; a bare number is no fiction day
    for found, source in ((fiction_day(stated) if isinstance(stated, str) else None, "delta"),
                          (state_day, "frontmatter"), (_heading_day(heading), "heading")):
        if found is not None:
            return found, source
    return None, None


def _heading_day(heading):
    if not heading:
        return None
    info = parse_beat_heading(heading.strip())
    return info["day"] if info else fiction_day(heading)


def rotate(project, session_file, *, day=None, turn_text=None, heading=None, beat=None, start=False,
           repair_only=False, allow_jump=False, today=None, config=None, log=True) -> dict:
    """Before a beat ships: the file it goes to, rotating to the next fiction day's file when the day moved.

    The beat's day comes from the turn's delta (gated, and tied to the anchor by
    the lint), else the session frontmatter's currentDate; a heading that
    disagrees is a warning, never a rotation. An earlier day stays in the current
    file, flagged: rotation only moves forward. start (/rp:start) also records
    the session's opening in the marker. Re-running after a crash at any step
    completes the rotation without repeating one.
    """
    project = Path(project)
    today = today or datetime.date.today().isoformat()
    config = config or load_config(project)
    named = _inside_sessions(project, str(session_file))
    if named is None or not named.is_file():
        raise ValueError(f"{session_file} is not a session file under {SESSION_DIR}/.")
    path, hops = _follow_rotation(project, named)
    rel = _relative(project, path)
    lost = rotation_footer(path)
    if lost:
        raise ValueError(f"{rel} was closed by rotation into {lost}, which is missing; it is never written "
                         "again. Ask the Keeper")
    result = {"action": "none", "path": rel, "previous": None, "day": None, "file_day": None,
              "repaired": None, "set_aside": [], "flag": None, "reason": None, "warnings": []}
    if hops:
        result["warnings"].append(f"{_relative(project, named)} was closed by rotation; using {rel}.")
    data = read_frontmatter(path)
    file_day, state_day = _days(data, None if data.get("log_day") is not None else _read_lines(path))
    interrupted = _interrupted(project, rel)
    if interrupted and _append_footer(project / interrupted, rel, file_day, today):
        result["repaired"] = interrupted
    if repair_only:  # /rp:end: close what an interrupted rotation left open, move nothing
        result["file_day"] = file_day
        result["set_aside"] = _set_aside_orphans(project, rel, data, today)
        if log and (result["repaired"] or result["set_aside"]):
            append_log(project, {"session": rel, "path": "rotate", "action": "none", "repaired": result["repaired"],
                                 "set_aside": result["set_aside"] or None})
        return result
    beat_day, source = _beat_day(day, turn_text, state_day, heading)
    result["day"], result["file_day"] = beat_day, file_day
    stated = _heading_day(heading)
    if stated is not None and beat_day is not None and source != "heading" and stated != beat_day:
        result["warnings"].append(f"The beat heading says Day {stated}, the gated state Day {beat_day}; the beat "
                                  f"goes by the state. Correct the heading.")
    if config["log"]["rotation"] == "off":
        result["reason"] = "rotation is off (sessionLog.rotation)"
    elif beat_day is None:
        result["reason"] = "no fiction day in the delta, the frontmatter's currentDate or the heading"
    elif file_day is None:
        result["reason"] = "the session file has no fiction day"
    elif beat_day < file_day:
        result["action"] = "step_back"  # no "Day N" in the flag: appended to a heading, it must not parse
        result["flag"] = (f"(STEP-BACK: a D{beat_day} beat in the D{file_day} file, a flashback or a numbering "
                          "slip; it stays here, and rotation never moves back)")
    elif beat_day - file_day > MAX_DAY_JUMP and not allow_jump:
        result["action"] = "refused"
        result["warnings"].append(f"The beat moves the day {beat_day - file_day} days, from Day {file_day} to Day "
                                  f"{beat_day}, and a rotation cannot be undone. Ask the Keeper; only on the Keeper's "
                                  "word that the jump is real does rotate run again with --allow-jump.")
    elif beat_day > file_day:
        _rotate_forward(project, path, rel, data, beat_day, today, beat, start, turn_text, result)
    # An earlier crash's unused day file continuing this one (after a rotation the new file is the marker's)
    result["set_aside"] = _set_aside_orphans(project, rel, data, today)
    if start and result["action"] in ("none", "step_back"):
        if str(data.get("status", "")).lower() == "closed":
            result["warnings"].append(f"{rel} was closed by /rp:end; the session reopens it. Set status: open in its "
                                      "frontmatter if play continues there.")
        _swap_marker(project, {"session_file": rel, "session_opened": rel})
    if start and result["action"] in ("none", "step_back", "rotated"):
        _append_line(project / result["path"], f"{OPENED_MARK} {today} -->")
    if log and (result["action"] != "none" or result["repaired"] or result["set_aside"]):
        append_log(project, {"session": result["path"], "path": "rotate", "action": result["action"],
                             "day": beat_day, "file_day": file_day, "previous": result["previous"], "beat": beat,
                             "step_back": True if result["action"] == "step_back" else None,
                             "repaired": result["repaired"], "set_aside": result["set_aside"] or None,
                             "start": True if start else None})
    return result


OPENED_MARK = "<!-- rp-session-opened:"


def _append_line(path: Path, line: str) -> None:
    """Append one line at the end of an open log (appending moves no earlier line)."""
    with open(path, "ab") as fh:
        size = fh.seek(0, os.SEEK_END)
        lead = ""
        if size:
            with open(path, "rb") as tail:
                tail.seek(size - 1)
                lead = "" if tail.read(1) == b"\n" else "\n"
        fh.write((lead + line + "\n").encode("utf-8"))


GENERATED_RE = re.compile(r"^<!-- rp-day-file: .*?; (\d+) lines as generated;")


def _untouched(path: Path) -> bool:
    """A day file exactly as rotation generated it: nothing written into it since (by line count).
    A file without the record, made by hand, never counts as untouched."""
    lines = _read_lines(path) or []
    found = next((m for line in lines[:HEADER_COPY_LIMIT] if (m := GENERATED_RE.match(line))), None)
    return found is not None and len(lines) == int(found.group(1))


def _set_aside_orphans(project: Path, rel: str, data: dict, today: str) -> list[str]:
    """Day files continuing rel that a crash left between publish and marker swap: unfooted, nothing
    written after their header, not named by the marker. Each is renamed to a hidden .orphan file
    (never deleted), so no fallback or later rotation takes it for the run's open file."""
    log_dir, _warning = _log_dir(project, data)
    if log_dir is None or not (project / log_dir).is_dir():
        return []
    named = _inside_sessions(project, _marker_session(project) or "")
    named = _relative(project, named) if named else None
    moved = []
    for candidate in sorted((project / log_dir).glob(DAY_FILE_GLOB)):
        candidate_rel = _relative(project, candidate)
        if (candidate_rel == named or _continues_target(project, read_frontmatter(candidate)) != rel
                or rotation_footer(candidate) or not _untouched(candidate)):  # cheapest test first
            continue
        target, n = candidate.with_name(f".{candidate.name}.orphan-{today}"), 1
        while target.exists():
            target, n = candidate.with_name(f".{candidate.name}.orphan-{today}-{n}"), n + 1
        os.rename(candidate, target)
        moved.append(candidate_rel)
    return moved


def _rotate_forward(project, path, rel, data, day, today, beat, start, turn_text, result) -> None:
    log_dir, warning = _log_dir(project, data)
    if log_dir is None:
        result["warnings"].append(warning)
        result["reason"] = "no log_dir"
        return
    target_rel = f"{log_dir}/{DAY_FILE_NAME.format(day)}"
    target = project / target_rel
    for stale in target.parent.glob(".day-*.md.tmp-*") if target.parent.is_dir() else ():
        stale.unlink(missing_ok=True)  # a publish killed before its cleanup
    delta, _parsed = parse_delta(parse_turn(turn_text).get("SCENE_STATE_DELTA", "")) if turn_text else ({}, True)
    stated = delta.get("currentDate")
    current_date = stated if isinstance(stated, str) and fiction_day(stated) == day else f"Day {day}"
    text = _day_file_text(path, rel, data, day, current_date, log_dir, today)
    if target.exists():
        if rotation_footer(target) or _continues_target(project, read_frontmatter(target)) != rel:
            result["action"] = "refused"
            result["warnings"].append(f"{target_rel} already exists and does not continue {rel}: rotation refuses to "
                                      "overwrite another day or reopen a closed file. Ask the Keeper.")
            return
        named = _inside_sessions(project, _marker_session(project) or "")
        in_use = named is not None and _relative(project, named) == target_rel
        if not in_use and _untouched(target):
            _publish(target, text, replace=True)  # an earlier attempt's copy, never used: rebuild it from now
    else:
        _publish(target, text)
    fields: dict = {"session_file": target_rel}
    if start:
        fields["session_opened"] = target_rel
    elif not (project / MARKER).exists():  # recreated mid-session: the session reaches back at least here
        fields["session_opened"] = rel
    _swap_marker(project, fields)
    _append_footer(path, target_rel, day, today, beat)
    result.update(action="rotated", path=target_rel, previous=rel)


# ------------------------------------------------------------ split (plan)
# Read-only: how existing logs would cut into day files, and where each old
# file:line citation would land. Nothing is written.

# rp_session_2026-10-17.md:786, `rp_session_2026-09-30.md` line 3197; 10-17:194, 10-07 log:395-397, 10-17 log l.9889
LINE_REF = r"(?::|,?\s+(?:lines?|l\.)\s*)(\d+)(?:[-–](\d+))?"
FULL_CITE_RE = re.compile(r"(rp_session_\d{4}-\d{2}-\d{2})(?:\.md)?`?" + LINE_REF)
SHORT_CITE_RE = re.compile(r"(?<![\d-])(\d{2}-\d{2})(?:\s+log)?" + LINE_REF)


def plan_split(project, logs: list[str], log_dir: str) -> dict:
    """Cut each log at fiction-day boundaries, as rotation would have; days that span logs join one file.

    Each day file would be: a generated preamble, then each part verbatim after a
    one-line source comment; a part opening at a log's line 1 has that log's
    frontmatter fenced as yaml. A step-back beat stays in the file it falls in.
    """
    project = Path(project)
    plan: dict = {"logs": [], "files": [], "step_backs": [], "no_day": [], "day_plans": []}
    files, top, texts = {}, None, {}
    for rel in logs:
        path = _inside_sessions(project, rel)
        lines = _read_lines(path) if path else None
        if path is None or lines is None:
            raise ValueError(f"{rel} is not a readable session file under {SESSION_DIR}/.")
        rel = _relative(project, path)
        texts[rel] = lines
        data = read_frontmatter(path)
        units = _beat_units(lines, _body_start(lines), _footer_start(lines))
        first = next((u["day"] for u in units if u["day"] is not None), None)
        first = first if first is not None else fiction_day(data.get("startDate")) or _file_day(data)
        if first is None:
            plan["logs"].append({"log": rel, "lines": len(lines), "beats": len(units), "days": []})
            continue
        top = first if top is None else max(top, first)
        cuts, day = [(0, top)], first
        if first < cuts[0][1]:
            plan["step_backs"].append({"log": rel, "line": 1, "beat": None, "day": first, "file_day": top})
        for unit in units:
            if unit["day"] is None:
                plan["no_day"].append({"log": rel, "line": unit["heading"], "beat": unit["beat"]})
            day = unit["day"] if unit["day"] is not None else day
            if day > top:
                cuts.append((unit["cut"], day))
                top = day
            elif day < top:
                plan["step_backs"].append({"log": rel, "line": unit["heading"], "beat": unit["beat"],
                                           "day": day, "file_day": top})
        fm_end = _frontmatter_end(lines)
        for n, (a, chunk_day) in enumerate(cuts):
            b = cuts[n + 1][0] if n + 1 < len(cuts) else len(lines)
            inside = [u for u in units if a <= u["cut"] < b]
            files.setdefault(chunk_day, []).append({
                "log": rel, "start": a + 1, "end": b, "beats": len(inside),
                "first_beat": inside[0]["beat"] if inside else None, "last_beat": inside[-1]["beat"] if inside else None,
                "fenced_frontmatter": a == 0 and fm_end is not None})
            for i in range(a, b):  # "Day Plan: Day 31 (into Day 32 waking)" belongs to Day 31 or 32
                named = {int(d) for m in FICTION_DAY_RE.finditer(lines[i]) for d in m.groups() if d} \
                    if DAY_PLAN_RE.match(lines[i]) else set()
                if named and chunk_day not in named:
                    plan["day_plans"].append({"log": rel, "line": i + 1, "day": max(named), "in_file_day": chunk_day})
        plan["logs"].append({"log": rel, "lines": len(lines), "beats": len(units), "days": [d for _, d in cuts]})
    days = sorted(files)
    mapping = {}
    for n, day in enumerate(days):
        target = f"{log_dir}/{DAY_FILE_NAME.format(day)}"
        previous = f"{log_dir}/{DAY_FILE_NAME.format(days[n - 1])}" if n else files[day][0]["log"]
        out = _split_preamble(day, previous, files[day], log_dir)
        for part in files[day]:
            out.append(f"<!-- split from {part['log']} lines {part['start']}-{part['end']} -->")
            lines, fm_end = texts[part["log"]], _frontmatter_end(texts[part["log"]])
            for i in range(part["start"] - 1, part["end"]):
                if part["fenced_frontmatter"] and i == 0:
                    out.append("```yaml")
                out.append(lines[i])
                mapping[(part["log"], i + 1)] = (target, len(out))
                if part["fenced_frontmatter"] and i == fm_end:
                    out.append("```")
        if n + 1 < len(days):
            following = f"{log_dir}/{DAY_FILE_NAME.format(days[n + 1])}"
            out += ["", "---", "", f"<!-- rp-rotated-to: {following} -->", f"**Continued in `{following}`**."]
        plan["files"].append({"day": day, "path": target, "parts": files[day], "lines": len(out),
                              "bytes": sum(len(x.encode("utf-8")) + 1 for x in out)})
    plan["_map"], plan["_texts"] = mapping, texts
    return plan


def _split_preamble(day: int, previous: str, parts: list[dict], log_dir: str) -> list[str]:
    sources = ", ".join(f"{p['log']}:{p['start']}-{p['end']}" for p in parts)
    return ["---", f'log_dir: "{log_dir}"', f"log_day: {day}", f'continues: "{previous}"',
            f'split_from: "{sources}"', "---", "",
            f"{DAY_BLOCK_START}: Day {day}; split from {sources} -->", f"# Day {day}", "",
            f"Continues `{previous}`.", DAY_BLOCK_END, ""]


def remap_citation(plan: dict, log: str, start: int, end: int | None = None) -> dict:
    """Where an old log:start(-end) citation would land, and whether the remap is exact."""
    lines = plan["_texts"].get(log)
    end = start if end is None else end
    if lines is None or start < 1 or end > len(lines) or end < start:
        return {"exact": False, "reason": "outside the log"}
    first, last = plan["_map"].get((log, start)), plan["_map"].get((log, end))
    if first is None or last is None or first[0] != last[0]:
        return {"exact": False, "reason": "crosses a day boundary"}
    if last[1] - first[1] != end - start:
        return {"exact": False, "reason": "spans the inserted frontmatter fence"}
    return {"exact": True, "path": first[0], "line": first[1], "end_line": last[1], "text": lines[start - 1]}


def scan_citations(project, plan: dict, roots: list[str]) -> dict:
    """Every rp_session_<date>.md:N and short <MM-DD>:N citation of the planned logs under roots."""
    by_name = {Path(rel).stem: rel for rel in plan["_texts"]}
    by_short = {m.group(1)[5:]: rel for rel in plan["_texts"] if (m := SESSION_DATE_RE.search(rel))}  # 10-17:194
    report = {"total": 0, "exact": 0, "reasons": {}, "by_file": {}, "not_exact": []}
    paths = []
    for root in roots:
        root = Path(root) if os.path.isabs(root) else Path(project) / root
        if root.is_file():
            paths.append(root)
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".") or d == ".claude"]
            paths += [Path(dirpath, f) for f in filenames if f.endswith((".md", ".txt", ".yaml", ".yml"))]  # not machine logs
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        found = [(by_name.get(m.group(1)), m) for m in FULL_CITE_RE.finditer(text)]
        found += [(by_short.get(m.group(1)), m) for m in SHORT_CITE_RE.finditer(text)]
        for log, m in found:
            if log is None:
                continue
            got = remap_citation(plan, log, int(m.group(2)), int(m.group(3)) if m.group(3) else None)
            stats = report["by_file"].setdefault(str(path), {"total": 0, "exact": 0})
            report["total"] += 1
            stats["total"] += 1
            if got["exact"]:
                report["exact"] += 1
                stats["exact"] += 1
            else:
                report["reasons"][got["reason"]] = report["reasons"].get(got["reason"], 0) + 1
                report["not_exact"].append({"file": str(path), "citation": m.group(0), "reason": got["reason"]})
    return report


# -------------------------------------------------------------------- CLI


def _session_for(project: Path, given):
    if given:
        return given, None
    resolved = resolve_session_file(project)
    return resolved["path"], resolved["warning"]


def _state_reports(project: Path, session: str, exists: bool = True):
    """The live state and the invariants report for one session file."""
    state = live_state(project, session) if exists else None
    return state, invariants_report(project, state)


def _warn_all(state, report) -> None:
    for text in (state or {}).get("warnings", []):
        print(f"WARNING: {text}", file=sys.stderr)
    for warning in (report or {}).get("warnings", []):
        print(f"WARNING: {warning['detail']}", file=sys.stderr)


def cmd_session(args) -> int:
    project = Path(args.project_dir)
    config = load_config(project, args.config)
    resolved = resolve_session_file(project, config=config)
    if resolved["warning"]:
        print(f"WARNING: {resolved['warning']}", file=sys.stderr)
    resolved["gate"] = {k: config[k] for k in ("review", "rewrites", "models", "log", "warnings")}
    exists = resolved["source"] != "new"
    resolved["live_state"], resolved["invariants"] = _state_reports(project, resolved["path"], exists)
    _warn_all(resolved["live_state"], resolved["invariants"])
    fields = _marker_fields(project) if resolved["source"] == "marker" else {}
    resolved["session_files"] = session_files(project, resolved["path"], fields.get("session_opened")) if exists else []
    resolved["session_opened_line"] = _opening_line(project, fields)
    resolved["continuity"] = continuity(project, resolved["path"], config["log"]["previousBeats"]) if exists else None
    interrupted = _interrupted(project, resolved["path"]) if exists else None
    resolved["rotation_warning"] = (
        f"The rotation from {interrupted} to {resolved['path']} was interrupted before {interrupted} got its "
        "footer; the next rp_gate.py rotate closes it.") if interrupted else None
    if interrupted:
        print(f"WARNING: {resolved['rotation_warning']}", file=sys.stderr)
    lines = _read_lines(project / resolved["path"]) if exists else None
    if lines and "<!-- rp-rotated-to: " in "\n".join(lines[:_footer_start(lines)]):
        print(f"WARNING: a rotation footer appears before the end of {resolved['path']} (quoted, or text was written "
              "after it closed); only a footer that ends a file closes it, so this file counts as open.", file=sys.stderr)
    print(json.dumps(resolved, ensure_ascii=False))
    return 0


def _opening_line(project: Path, fields: dict):
    """The line the session opened at: the last opening comment /rp:start appended to that file.
    Frontmatter deltas and header edits above it move it, so a stored line number would drift."""
    opened = _inside_sessions(project, str(fields.get("session_opened") or ""))
    lines = (_read_lines(opened) or []) if opened else []
    found = next((i + 1 for i in range(len(lines) - 1, -1, -1) if lines[i].startswith(OPENED_MARK)), None)
    line = fields.get("session_opened_line")  # an older marker's record
    return found if found is not None else line if isinstance(line, int) else None


def cmd_rotate(args) -> int:
    project = Path(args.project_dir)
    session, warning = _session_for(project, args.session_file)
    if warning:
        print(f"WARNING: {warning}", file=sys.stderr)
    text = None
    if args.turn_file:
        try:
            text = sys.stdin.read() if args.turn_file == "-" else Path(args.turn_file).read_text(encoding="utf-8")
        except OSError as exc:
            print(f"ERROR: cannot read the turn file: {exc}", file=sys.stderr)
            return 2
    try:
        result = rotate(project, session, day=args.day, turn_text=text, heading=args.heading, beat=args.beat,
                        start=args.start, repair_only=args.repair, allow_jump=args.allow_jump,
                        config=load_config(project, args.config), log=not args.no_log)
    except ValueError as exc:
        print(f"ERROR: {exc}.", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"ERROR: rotation failed: {exc}. Run the same command again; every step is safe to repeat.",
              file=sys.stderr)
        return 2
    for text in result["warnings"]:
        print(f"WARNING: {text}", file=sys.stderr)
    print(json.dumps(result, ensure_ascii=False))
    return 1 if result["action"] == "refused" else 0


def cmd_split(args) -> int:
    project = Path(args.project_dir)
    try:
        plan = plan_split(project, args.log, args.log_dir)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    out = {k: v for k, v in plan.items() if not k.startswith("_")}
    if args.cite:
        out["citations"] = scan_citations(project, plan, args.cite)
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


def cmd_invariants(args) -> int:
    project = Path(args.project_dir)
    if args.session_file:
        session, exists = args.session_file, True
    else:
        resolved = resolve_session_file(project)
        session, exists = resolved["path"], resolved["source"] != "new"
    state, report = _state_reports(project, session, exists)
    if report is None:
        print(f"ERROR: {INVARIANTS} does not exist. Run /rp:extract-invariants.", file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=1))
    live = any(w["kind"] == "live_state" for w in report["warnings"])
    return 1 if args.strict and live else 0


def cmd_check(args) -> int:
    project = Path(args.project_dir)
    try:
        text = sys.stdin.read() if args.turn_file == "-" else Path(args.turn_file).read_text(encoding="utf-8")
    except OSError as exc:
        print(f"ERROR: cannot read the turn file: {exc}", file=sys.stderr)
        return 2
    session, warning = _session_for(project, args.session_file)
    if warning:
        print(f"WARNING: {warning}", file=sys.stderr)
    config = load_config(project, args.config)
    scene = load_scene(project, session)
    started = time.perf_counter()
    _state, invariants = _state_reports(project, session)
    invariant_kinds = [w["kind"] for w in invariants["warnings"]] if invariants else []
    turn = parse_turn(text)
    result = lint(turn, scene, config, project)
    triggers = beat_triggers(turn, result, scene, config)
    rewrite = args.rewrite or args.attempt > 1  # a later attempt answers a confirmed violation
    decision = route(result, triggers, config, recheck=args.recheck, final=args.final, rewrite=rewrite)
    elapsed = round((time.perf_counter() - started) * 1000, 2)
    common = {"session": session, "beat": args.beat, "attempt": args.attempt}
    if not args.no_log:
        append_log(project, {**common, "path": "lint", "findings": [f["check"] for f in result["findings"]],
                             "triggers": triggers, "class": decision["beat"], "next": decision["next"],
                             "recheck": args.recheck or None, "final": args.final or None,
                             "rewrite": rewrite or None, "scene_warning": scene["warning"],
                             "invariants_warnings": invariant_kinds or None, "elapsed_ms": elapsed})
        if decision["next"] == "ship" and not args.final:
            reason = "policy lint-only" if config["review"] == "lint-only" else "routine beat, lint clean"
            append_log(project, {**common, "path": "skipped", "reason": reason})
    print(json.dumps({
        "session_file": session,
        "session_warning": warning,
        "scene_warning": scene["warning"],
        "lint": {k: result[k] for k in ("findings", "events", "present", "delta", "elapsed_ms")},
        "triggers": triggers,
        "route": decision,
        "elapsed_ms": elapsed,
        "invariants": invariants,
        "config_warnings": config["warnings"],
        "logged": not args.no_log,
    }, ensure_ascii=False, indent=1))
    return 0


def cmd_log(args) -> int:
    if args.path not in LOG_PATHS:
        print(f"ERROR: --path must be one of {', '.join(LOG_PATHS)}", file=sys.stderr)
        return 2
    project = Path(args.project_dir)
    session, _warning = _session_for(project, args.session_file)
    line = append_log(project, {
        "session": session, "beat": args.beat, "path": args.path, "model": args.model,
        "attempt": args.attempt, "attempts": args.attempts, "verdict": args.verdict,
        "violation_categories": _csv(args.violations), "clean_categories": _csv(args.clean), "note": args.note,
    })
    print(json.dumps(line, ensure_ascii=False))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="rp_gate.py", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    s = sub.add_parser("session", help="print the live session file and the gate settings")
    s.add_argument("--project-dir", default=".")
    s.add_argument("--config", help=f"mode file (default: {MODE_FILE})")
    c = sub.add_parser("check", help="lint and route one GM turn")
    c.add_argument("--project-dir", default=".")
    c.add_argument("--session-file")
    c.add_argument("--turn-file", required=True, help="the GM turn file, or - for stdin")
    c.add_argument("--config", help=f"mode file (default: {MODE_FILE})")
    c.add_argument("--attempt", type=int, default=1)
    c.add_argument("--beat")
    c.add_argument("--recheck", action="store_true", help="re-lint after the edit pass fixed mechanical findings")
    c.add_argument("--final", action="store_true", help="re-lint after the reviewer's fixes: ship or fix only")
    c.add_argument("--rewrite", action="store_true", help="this draft answers a confirmed violation: review it")
    c.add_argument("--no-log", action="store_true")
    g = sub.add_parser("log", help="append one gate line to the validator log")
    g.add_argument("--project-dir", default=".")
    g.add_argument("--session-file")
    g.add_argument("--path", required=True)
    g.add_argument("--model")
    g.add_argument("--verdict")
    g.add_argument("--violations")
    g.add_argument("--clean")
    g.add_argument("--attempt", type=int)
    g.add_argument("--attempts", type=int)
    g.add_argument("--beat")
    g.add_argument("--note")
    v = sub.add_parser("invariants", help=f"report live state in {INVARIANTS} and its age against the run's state")
    v.add_argument("--project-dir", default=".")
    v.add_argument("--session-file", help="the session whose state dates the run (default: the live session)")
    v.add_argument("--strict", action="store_true", help="exit 1 when the invariants carry live state")
    r = sub.add_parser("rotate", help="before a beat ships: the file it goes to, rotating on a later fiction day")
    r.add_argument("--project-dir", default=".")
    r.add_argument("--session-file", help="the live session file (default: from the marker)")
    r.add_argument("--turn-file", help="the gated turn file; its delta's currentDate is the beat's day")
    r.add_argument("--day", type=int, help="the fiction day explicitly (/rp:start)")
    r.add_argument("--heading", help="the beat heading the command will write; checked against the day")
    r.add_argument("--beat", help="the beat label, for the footer and the log")
    r.add_argument("--start", action="store_true", help="/rp:start: record the session's opening in the marker")
    r.add_argument("--repair", action="store_true", help="/rp:end: only close a file an interrupted rotation left open")
    r.add_argument("--allow-jump", action="store_true",
                   help=f"rotate past more than {MAX_DAY_JUMP} days: only on the Keeper's word")
    r.add_argument("--config", help=f"mode file (default: {MODE_FILE})")
    r.add_argument("--no-log", action="store_true")
    p = sub.add_parser("split", help="plan, read-only, how existing logs would cut into day files")
    p.add_argument("--project-dir", default=".")
    p.add_argument("--log", action="append", required=True, help="a log to plan, in play order (repeatable)")
    p.add_argument("--log-dir", required=True, help=f"the day-file directory, e.g. {SESSION_DIR}/<campaign>/log")
    p.add_argument("--cite", action="append", help="a file or directory to scan for citations (repeatable)")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0) if exc.code not in (None, 0) else 0
    commands = {"session": cmd_session, "check": cmd_check, "log": cmd_log, "invariants": cmd_invariants,
                "rotate": cmd_rotate, "split": cmd_split}
    return commands[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
