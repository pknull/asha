#!/usr/bin/env python3
"""Per-beat RP continuity gate: session resolution, beat lint, routing, logging.

Standard library only and no model calls. /rp:turn runs `check` on every
draft; it decides whether a model reviewer is worth spending on the beat.

  session     print the live session file (marker first, never the clock), where
              its live state is, the day's continuity read, and any warnings
  turn        write the gate's turn file from the GM's final message: its five blocks, in order, each once
  check       lint one GM turn, route it, append the lint (and skipped) lines
  rotate      before a beat ships: move to the next fiction day's log file, then write the turn's struck drafts
  strike      keep a draft the gate rejected, for rotate to write as a STRUCK block
  ship        append the beat and apply its gated delta, in one write; --provisional ships it before
              its review, which runs in the background on a frozen copy of what it was drafted against
  settle      apply a background review's verdict to the provisional beat it reviewed
  pending     the provisional beats whose review is not yet settled
  brief       build the turn's GM brief from disk: current state, Day Plan, recent beats, keyed entries
  state       the run: block's health; --refresh copies it into the run state file
  summary     the days a session closed; --publish one day's summary; --stitch the session's
  log         append one review, adjudicate or turn line to the validator log
  invariants  report live state in Memory/invariants.md and its staleness
  split       plan, read-only, how existing logs would cut into day files

The lint checks only what can be checked mechanically: the closing
Scene/Location/Present anchor, named actors against the scene's people,
SCENE_STATE_DELTA keys and their agreement with the anchor, and the form of
each typed run state change. Everything that needs judgement belongs to the
continuity reviewer.

Current state is never read from the invariants: it lives in the session's
frontmatter and header and in the run's state file, which the session names.

One log file per fiction day: when a beat's day is later than its file's, the
file closes with a footer and the next day's file opens with the header copied
as written. A closed file is never edited again, so file:line citations hold.

A draft the gate rejects is kept, never as story: strike stashes it, and rotate
writes it into the file its beat goes to as a STRUCK callout that every reader
here skips.

A provisional beat (shipped before its review, on a harness that can run the
review in the background) is marked in its file until settle applies the
verdict: clean or soft drops the marks, a confirmed hard finding strikes it and
unwinds its delta. Nothing ships, rotates or is summarised over it meanwhile.
"""
from __future__ import annotations

import argparse
import bisect
import copy
import datetime
import difflib
import errno
import glob
import hashlib
import itertools
import json
import os
import re
import shutil
import stat
import sys
import time
import unicodedata
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
    "background": True,
    "briefBytes": 80_000,  # about 33k tokens of a live log (B4355): read in the parts the report's reads name
}
# Harnesses where a command can run a subagent in the background and collect its result later. Claude Code only:
# docs/session-hub.md records Claude's background agents; nothing in asha verifies it for Codex, Copilot or OpenCode,
# where /rp:turn keeps the review before the beat is shown.
BACKGROUND_HARNESSES = ("claude",)
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
ROLE_LABEL_MAX = 200  # a longer participant role is the scene's narrative, rewritten every beat
# Stats a narrative PC role may carry ("POW 53/60", "Luck 23"); a session's run.stats names are added.
STAT_NAMES = ("STR", "CON", "SIZ", "DEX", "APP", "INT", "POW", "EDU", "HP", "MP", "SAN", "Luck")
LOG_PATHS = ("review", "adjudicate", "turn")  # check writes lint and skipped; ship, strike and settle their own
SECTIONS = ("KEEPER_INPUT", "DRAFT", "GM_SPAWN_LOG", "SOURCE_LOG", "SCENE_STATE_DELTA")

# ---------------------------------------------------------------- YAML subset
# Enough YAML for frontmatter, deltas and a mode-file block: mappings, block
# sequences, flow lists, quoted and plain scalars, comments.


def _strip_comment(line: str) -> str:
    out, quote, i = [], None, 0
    while i < len(line):
        ch = line[i]
        if quote and ((quote == '"' and ch == "\\") or (quote == "'" and line[i:i + 2] == "''")):
            out.append(line[i:i + 2])  # an escaped quote does not close the scalar
            i += 2
            continue
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'" and (i == 0 or line[i - 1] in " \t[,{:-"):
            quote = ch
        elif ch == "#" and (i == 0 or line[i - 1] in " \t"):
            break
        out.append(ch)
        i += 1
    return "".join(out).rstrip()


def _split_flow(text: str) -> list[str]:
    parts, buf, quote, depth, escaped = [], [], None, 0, False
    for ch in text:
        if escaped:
            escaped = False
        elif quote == '"' and ch == "\\":
            escaped = True
        elif quote:
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


KEY_RE = re.compile(r"""^("(?:[^"\\]|\\.)*"|'(?:[^']|'')*'|[^\s"'\-#][^:]*?|-[^\s][^:]*?)\s*:(?:\s+(.*)|)$""")


def _split_key(text: str):
    m = KEY_RE.match(text)
    if not m:
        return None, None
    key = m.group(1)
    if key[:1] == '"':
        try:
            key = json.loads(key)
        except ValueError:
            key = key[1:-1]
    elif key[:1] == "'":
        key = key[1:-1].replace("''", "'")
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
        if key is not None and item[:1] not in "\"'[{":  # "- {a: b}" is a flow mapping, not a key "{a"
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


YAML_BREAK_RE = re.compile(r"\r\n|\n|\r")  # YAML's line breaks; a U+2028 in a value is not one


def parse_yaml(text: str):
    rows = []
    for raw in YAML_BREAK_RE.split(text):
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


# Writing the same subset back: strings as JSON (a double-quoted YAML scalar
# the parser above reads exactly), on and off bare, mappings and lists as blocks.
BARE_KEY_RE = re.compile(r"[A-Za-z0-9_][\w .()/+-]*")
LINE_BREAKS = "\n\r\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029"  # where str.splitlines breaks a line


def _yaml_scalar(value) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if value in ("on", "off"):
        return value
    return _json(str(value))


def _json(value) -> str:
    """JSON text with the line separators json.dumps leaves raw escaped: the gate's line counts split there."""
    text = json.dumps(value, ensure_ascii=False)
    return text.replace("\x85", "\\u0085").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def _yaml_key(key) -> str:
    key = str(key)
    if any(ch in key for ch in LINE_BREAKS + "\t"):
        raise ValueError(f"the name {key!r} cannot be written as a YAML key here")
    if BARE_KEY_RE.fullmatch(key) and not key.endswith(" "):
        return key
    if '"' not in key and "\\" not in key:
        return f'"{key}"'
    return "'" + key.replace("'", "''") + "'"  # single quotes take a double quote or a backslash as written


def emit_yaml(key, value, indent: int = 0) -> list[str]:
    """One key and its value as YAML lines, read back exactly by parse_yaml."""
    head = " " * indent + _yaml_key(key) + ":"
    if isinstance(value, dict) and value:
        return [head + "\n"] + [line for k, v in value.items() for line in emit_yaml(k, v, indent + 2)]
    if isinstance(value, list) and value:
        return [head + "\n"] + [line for item in value for line in _emit_item(item, indent + 2)]
    if isinstance(value, (dict, list)):
        return [head + (" {}\n" if isinstance(value, dict) else " []\n")]
    return [f"{head} {_yaml_scalar(value)}\n"]


def _emit_item(item, indent: int) -> list[str]:
    pad = " " * indent
    if isinstance(item, dict) and item and _yaml_key(next(iter(item))) == str(next(iter(item))):
        lines = [line for k, v in item.items() for line in emit_yaml(k, v, indent + 2)]
        return [pad + "- " + lines[0][indent + 2:]] + lines[1:]
    if isinstance(item, (dict, list)):  # a flow collection; also a mapping whose first key needs quotes ("- 'k': v"
        return [pad + "- " + _json(item) + "\n"]  # reads as a scalar item)
    return [f"{pad}- {_yaml_scalar(item)}\n"]


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
STRUCK_RE = re.compile(r"(?i)^>\s?\[!struck\]")  # a struck draft's callout; it ends at the first line without ">"
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
        end, ended_by, truncated, counted, in_struck = start - 1, "eof", False, 0, False
        for n, line in itertools.chain(pending, numbered):  # lazily: a log runs to megabytes
            if HEADER_END_RE.match(line):
                ended_by = line.strip()[:80]
                break
            in_struck = bool(STRUCK_RE.match(line)) or (in_struck and line.startswith(">"))
            counted += 0 if in_struck else 1  # struck drafts are no header, and do not use up its lines
            if counted > HEADER_MAX_LINES:
                truncated = True
                break
            end = n
            layout["text"].append(line.rstrip("\n"))
    if end >= start:
        text = layout["text"]
        struck = struck_spans(text)
        header = _skip_struck({"start": start, "end": end}, text, struck, first=start)
        if header:
            header.update(ended_by=ended_by, truncated=truncated)
        layout["header"] = header
        hidden = {i for s, e in struck for i in range(s, e)}
        layout["text"] = [line for i, line in enumerate(text) if i not in hidden]
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


def _inside_project(project: Path, raw: str, suffixes=(".md",)):
    """The file a pointer names, if it stays inside the project (lexically) and has one of the suffixes
    (any, when suffixes is None)."""
    root = os.path.abspath(project)
    path = os.path.abspath(os.path.join(root, raw))
    if (suffixes and not path.endswith(suffixes)) or path == root or os.path.commonpath([root, path]) != root:
        return None
    return Path(path)


def _section_bounds(lines: list[str], name: str):
    """(heading index, end index) of the section a heading names, or None. Fenced headings do not count."""
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
    return start, next((i for i, lvl, _ in headings if i > start and lvl <= level), len(lines))


def _section(path: Path, name: str):
    """A heading's line range and its first entry (the current one by convention)."""
    try:
        lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    except OSError:
        return None
    bounds = _section_bounds(lines, name)
    if bounds is None:
        return None
    start, end = bounds
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
    generated = current is not None and lines[j].startswith(STATE_START)  # the block state --refresh writes
    return {"heading_line": start + 1, "end_line": end, "current": current, "generated": generated}


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

# a block header: the name, with or without a heading mark or bold, followed by a colon, or alone on its line
# (the GM writes "DRAFT" bare, B4393 and B4394); a name inside a sentence is prose
SECTION_RE = re.compile(r"^\s*(?:#+\s*)?(?:\*\*)?(%s)(?:\*\*)?\s*(?::(?:\*\*)?\s*(.*))?$" % "|".join(SECTIONS))


def parse_turn(text: str) -> dict:
    """Split a GM turn file into its blocks. Text before any header is the draft."""
    sections, preamble, current, buf = {}, [], None, []
    for line in YAML_BREAK_RE.split(text):
        if line.strip().startswith("```"):
            continue
        m = SECTION_RE.match(line)
        if m:
            if current:
                sections[current] = "\n".join(buf).strip()
            current, rest = m.group(1), (m.group(2) or "").strip()
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
          "Their", "Theirs", "Our", "Ours", "Your", "Yours", "My", "Mine", "This", "That", "These", "Those",
          # role labels are not names: "PC move (B4378)", a file stem "New PC - Listener Recruit"
          "PC", "NPC", "GM", "DM"}
# An epithet is one name, not a list of people: "Shah of Fire Serpents", "Hypnos, the Dreamweaver",
# "New PC - Listener Recruit". Its words match only together; turnGate.cast takes a short form.
EPITHET_RE = re.compile(r"\s(?:of|the|and|in|at|to|on|for|from|with|by|de|du|von|van|der|den|del|della|di|da"
                        r"|la|le|el|al|bin|ibn)\s|,|\s[-\u2013\u2014]\s")
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
    narrative = [str(item.get("name", "")) for item in raw
                 if isinstance(item, dict) and len(str(item.get("role") or "")) > ROLE_LABEL_MAX]
    warning = None
    if not data:
        warning = f"{session_file} has no readable YAML frontmatter; the scene starts empty."
    elif not raw:
        warning = f"{session_file} frontmatter lists no participants."
    return {
        "participants": _participant_names(raw),
        "pc": pc,
        "pc_role": pc_role,
        "narrative_roles": narrative,
        "time": scene.get("time", data.get("currentTime")),
        "day": fiction_day(data.get("currentDate")),
        "log_day": _log_day(data),
        "run": data.get("run") if "run" in data else None,  # absent: run state is free text
        "session_dir": str((path if path.is_absolute() else project / path).parent),
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
        epithet = bool(EPITHET_RE.search(" ".join(re.split(r"[-\s]+", full)) if full == full.lower() else full))
        if full and full == full.lower():  # a file stem like "ildric-marr"
            full = " ".join(w[:1].upper() + w[1:] for w in re.split(r"[-\s]+", full) if w)
        if len(name_tokens(full)) > 1 and " " in full:
            variants.update({full, full.upper()})
        titled = re.match(r"(?i)(the|a|an)\s+(.*)", full)
        if titled:  # "The Frost Keeper" is a title: its words are not names
            variants.update({full, full.upper(), f"the {titled.group(2)}", titled.group(2)})
            continue
        if epithet:  # "Shah of Fire Serpents": "Fire" is not a person
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


def _page(lines: list[str], keeper: str) -> str:
    """The prose the presence check reads.

    The GM's working before the beat's heading (a PRE-COMMIT block: its notes, its
    STATE, "LISS (offstage): ...") is not the page, and nor is a heading; without a
    beat heading the whole draft is. The Keeper's input echoed as the PC's move
    ("**PC move (B4377):** ...") is the Keeper's, read like a quotation.
    """
    last = max((i for i, line in enumerate(lines) if parse_beat_heading(line)), default=-1)
    text = "\n".join(line for line in lines[last + 1:] if not HEADING_RE.match(line))
    words = keeper.split()
    if words:
        echo = re.compile(r"(?m)^[ \t]*(?:[*_]*[^\n:*_]{1,40}:[*_]*[ \t]*)?(%s)[ \t]*$" % r"\s+".join(map(re.escape, words)))
        text = echo.sub(lambda m: m.group(0)[:m.start(1) - m.start(0)] + " «Q». ", text)
    return text


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


def _turn_day(delta: dict, scene_field, fallback):
    """The beat's fiction day, as the lint and ship both take it: the delta's currentDate, else the anchor's
    Scene: Day N, else the fallback (the session's day)."""
    stated = delta.get("currentDate")
    day = fiction_day(stated) if isinstance(stated, str) else None
    m = ANCHOR_DAY_RE.match(scene_field or "") if day is None else None
    if m:
        day = int(m.group(3) or m.group(2) or m.group(1))
    return day if day is not None else fallback


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
        elif key == "run" and isinstance(value, dict):  # run: {workings: {HOMING: on}}
            for section, entries in value.items():
                if isinstance(entries, dict):
                    delta.update({f"run.{section}.{name}": v for name, v in entries.items()})
                else:
                    delta[f"run.{section}"] = entries
        elif key.startswith("run.") and key.count(".") == 1 and isinstance(value, dict):  # run.workings: {...}
            delta.update({f"{key}.{name}": v for name, v in value.items()})
        else:
            delta[key] = value
    return delta, True


# ------------------------------------------------------------- run state
# Typed run state, opt-in: a run: block in the session frontmatter beside
# scene:. Its four sections hold the PC's stats, body facts, standing workings
# (on or off) and clocks (the day each fires, and what fires). The gated delta
# changes it (run.<section>.<name>) and ship applies it with the beat. A session
# without the block keeps its run state as free text, as before.

RUN_SECTIONS = ("stats", "body", "workings", "clocks")
TOGGLES = ("on", "off")
CLOCK_FIELDS = ("day", "fires", "time")
MAX_CLOCK_AHEAD = 366  # a clock set further ahead than this is a typo
STAT_PAIR_RE = re.compile(r"\s*-?\d+\s*/\s*\d+\s*")  # "54/60": current and maximum


def _stat_shape(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return "int"
    return "pair" if isinstance(value, str) and STAT_PAIR_RE.fullmatch(value) else None


def _good_name(name) -> bool:
    return (isinstance(name, str) and 0 < len(name) <= 80 and name == name.strip()
            and not any(ch in name for ch in LINE_BREAKS + "\t"))


def _near(name: str, names) -> str | None:
    fold = {re.sub(r"[\W_]+", "", str(n)).lower(): n for n in names}
    found = fold.get(re.sub(r"[\W_]+", "", name).lower())
    if found is None:
        close = difflib.get_close_matches(name, [str(n) for n in names], n=1, cutoff=0.8)
        found = close[0] if close else None
    return found


def _entry_problem(section: str, name: str, value, entries: dict, standing: bool = False):
    """(check, detail) for one run entry's type, or None. standing: an entry already in the block."""
    where = f"run.{section}.{name}"
    if section == "stats":
        want, got = (None if standing else _stat_shape(entries.get(name))), _stat_shape(value)
        if got is None or (want is not None and got != want):
            forms = {"int": "an integer, as now", "pair": 'a "current/max" string such as "54/60", as now'}
            form = forms.get(want or "", "an integer or current/max")
            return "run_bad_value", f"{where} is {value!r}; a stat is {form}."
    elif section == "body":
        if value is not None and not (isinstance(value, str) and value.strip()
                                      and not any(ch in value for ch in LINE_BREAKS)):
            return "run_bad_value", f"{where} is {value!r}; a body fact is one line of text (or null: the part is gone)."
    elif section == "workings":
        if value not in TOGGLES or not isinstance(value, str):
            return "run_bad_value", f"{where} is {value!r}; a working is toggled only to on or off."
    elif section == "clocks" and value is not None:
        if (not isinstance(value, dict) or set(value) - set(CLOCK_FIELDS) or "day" not in value
                or "fires" not in value):
            return "run_bad_value", (f"{where} is {value!r}; a clock is {{day: N, fires: \"what happens\"}} "
                                     "(time optional), or null once it has fired.")
        day, fires, at = value.get("day"), value.get("fires"), value.get("time")
        if isinstance(day, bool) or not isinstance(day, int) or not (isinstance(fires, str) and fires.strip()) \
                or (at is not None and not isinstance(at, str)):
            return "run_bad_value", f"{where}: day is a whole fiction day and fires says what happens."
    return None


def run_delta_findings(delta: dict, run, beat_day=None):
    """(findings, events): the deterministic checks on each run.* entry of a delta.

    Each finding is mechanical (fix: edit) and carries run: true, so it never
    sends a beat to the model reviewer on its own; ship drops what still fails.
    """
    keys = [k for k in delta if k == "run" or k.startswith("run.")]
    findings, events = [], []
    for key in keys:
        def found(check, detail, typed=True):
            findings.append({"check": check, "fix": "edit", "run": typed, "key": key, "detail": detail})
        if not isinstance(run, dict):  # an unknown key, as before typed run state: routed like one
            found("run_no_schema", f"{key}: this session keeps its run state as free text (no run: block in "
                                   "its frontmatter). Drop the key; the prose and the run state file carry it.",
                  typed=False)
            continue
        parts = key.split(".", 2)
        if len(parts) < 3 or parts[1] not in RUN_SECTIONS:
            found("run_unknown_field", f"{key}: run state has the sections {', '.join(RUN_SECTIONS)}; write "
                                       "run.<section>.<name>.")
            continue
        _, section, name = parts
        if not _good_name(name):
            found("run_bad_name", f"{key}: a name holds no tab or line break, and is 1-80 characters with no "
                                  "space at either end.")
            continue
        entries = run.get(section)
        entries = entries if isinstance(entries, dict) else {}
        value, exists = delta[key], name in entries
        near = None if exists else _near(name, entries)
        hint = f" Did you mean {near!r}?" if near else ""
        if section == "stats" and not exists:
            found("run_unknown_field", f"{key}: no stat {name!r} in the run block.{hint} A new stat is the "
                                       "Keeper's, written between turns.")
            continue
        problem = _entry_problem(section, name, value, entries)
        if problem:
            found(*problem)
        elif value is None and not exists:
            found("run_unknown_field", f"{key}: nothing named {name!r} to clear.{hint}")
        elif section == "workings" and not exists and value == "off":
            found("run_unknown_field", f"{key}: no working {name!r} is on to turn off.{hint}")
        elif section == "clocks" and value is not None and beat_day is not None \
                and not beat_day <= value["day"] <= beat_day + MAX_CLOCK_AHEAD:
            found("run_clock_day", f"{key}: Day {value['day']} is not between the beat's Day {beat_day} and "
                                   f"Day {beat_day + MAX_CLOCK_AHEAD}.")
        elif not exists and value is not None:
            events.append({"kind": "run_added", "key": key})
    return findings, events


def standing_run_warnings(run) -> list[str]:
    """What is wrong with a session's run: block as it stands (the Keeper or the GM may edit it by hand)."""
    if run is None:
        return []
    if not isinstance(run, dict):
        return ["run: in the session frontmatter must be a mapping of stats, body, workings and clocks."]
    warnings = []
    for section, entries in run.items():
        if section not in RUN_SECTIONS:
            warnings.append(f"run.{section} is not a run state section ({', '.join(RUN_SECTIONS)}).")
            continue
        if entries is None:
            continue
        if not isinstance(entries, dict):
            warnings.append(f"run.{section} must be a mapping of names to values.")
            continue
        for name, value in entries.items():
            problem = (("run_bad_name", f"run.{section}.{name}: the name cannot be written back.")
                       if not _good_name(name) else _entry_problem(section, name, value, entries, standing=True))
            if problem:
                warnings.append(problem[1])
    return warnings


def run_notes(run, beat_day, delta=None) -> list[dict]:
    """Clocks past due at the beat's day that the delta neither clears nor moves: a note, never a finding."""
    clocks = run.get("clocks") if isinstance(run, dict) else None
    clocks = clocks if isinstance(clocks, dict) else {}
    notes = []
    for name, value in clocks.items():
        key = f"run.clocks.{name}"
        if delta and key in delta:
            continue
        day = value.get("day") if isinstance(value, dict) else None
        if beat_day is not None and isinstance(day, int) and not isinstance(day, bool) and day < beat_day:
            notes.append({"key": key, "day": day})  # fire it in the fiction, then clear (null) or move it
    return notes


def lint(turn: dict, scene: dict, config: dict, project, brief=None) -> dict:
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
    body_lines = [line[:ANCHOR_START_RE.search(line).start()] if ANCHOR_LINE_RE.search(line) else line
                  for line in draft.splitlines()]
    body = "\n".join(body_lines)
    narration = QUOTE_RE.sub(_quote, body.replace("*", "").replace("_", ""))
    page = QUOTE_RE.sub(_quote, _page(body_lines, turn.get("KEEPER_INPUT", "")).replace("*", "").replace("_", ""))
    tokens_re = _tokens_regex(_cast(project, config, from_start + present))
    hard = set()  # names a common word cannot excuse: the scene's people and turnGate.cast
    for name in from_start + present + list(config["cast"]):
        hard |= name_tokens(name) | {_plain(name)}
    done = set()
    lowercase = set(re.findall(r"\b[a-z][\w'’-]*", narration))
    sentences = [s for s in SENTENCE_RE.split(page) if s.strip()] if tokens_re else []
    for index, sentence in enumerate(sentences):
        after_quote = index > 0 and sentences[index - 1].rstrip().endswith("«Q».")
        for m in tokens_re.finditer(sentence):
            name = m.group(0)
            if name.lower() in done or any(same_person(name, p) for p in from_start):
                continue
            if (not sentence[:m.start()].strip() and not after_quote and not name.isupper()
                    and name.lower() in lowercase and name.lower() not in hard):
                continue  # "Frost runs along the wall" where the draft also says "frost"
            if name.isupper() and name.lower() in lowercase and name.lower() not in hard:
                continue  # "BY THE FIRE" in the GM's capitals, where the draft also says "fire"
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
    unknown = sorted(k for k in delta if k not in known and k not in ROOT_MIRRORS
                     and k != "run" and not k.startswith("run."))  # run.* has its own checks, below
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

    # Run state: each run.* entry against the session's run: block, the beat's day bounding clocks.
    beat_day = _turn_day(delta, fields.get("scene") if fields else None, scene.get("day"))
    run_findings, run_events = run_delta_findings(delta, scene.get("run"), beat_day)
    findings += run_findings
    events += run_events
    role, before = _pc_delta_role(scene, delta), str(scene.get("pc_role") or "")
    if role is not None and max(len(role), len(before)) > ROLE_LABEL_MAX:
        events.append({"kind": "pc_role_narrative", "chars": len(role), "before_chars": len(before)})
    if scene.get("narrative_roles") and "scene.participants" not in delta:  # they go stale: see roleplay-gm
        events.append({"kind": "roles_narrative", "names": list(scene["narrative_roles"])})

    # SOURCE_LOG: each sourced quote against the file it names.
    session_dir = scene.get("session_dir")
    sources = source_quotes(turn, project, Path(session_dir) if session_dir else None, brief=brief)
    findings += [{"check": "source_unverified", "fix": "review", "n": q["n"], "file": q["file"],
                  "detail": f"SOURCE_LOG sourced #{q['n']}: {q['reason']}."}
                 for q in sources["quotes"] if not q["verified"]]
    return {
        "findings": findings,
        "events": events,
        "run_notes": run_notes(scene.get("run"), beat_day, delta),
        "present": present,
        "anchored": bool(fields),
        "prior": scene["participants"],
        "delta": delta,
        "sources": sources,
        "narration": narration,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
    }


# ---------------------------------------------------------- source quotes
# SOURCE_LOG's sourced entries name a file and quote it. The lint finds each
# quote in its file, so nobody re-derives line numbers: the quote is the
# locator, and the check reports the line it is on. A quote it cannot find is
# a lead for the reviewer (source_fidelity), never an edit: only the source can
# say what it says. Quote marks (escaped or not), emphasis, dashes, case and
# spacing do not count. " / " joins separate excerpts, each found on its own in
# the file or in one of the files the entry names; "...", "…" or "[...]" marks
# a cut inside an excerpt, whose pieces must come in order and close together.
# What is not a source never verifies: a struck draft, a session's frontmatter
# (the state being written), the gate's scratch and its log. A quote found in
# an earlier beat of a log is marked history: play, not something written down.

SOURCE_FILE_MAX = 16 * 1024 * 1024
SOURCE_CUT_MAX = 600  # normalised characters a cut may leave out between two pieces
SOURCE_SUFFIX_RE = re.compile(r"^(.*?\.(?:md|markdown|txt|ya?ml|jsonl?|csv))(?![\w.])", re.I)
SOURCE_GROUP_RE = re.compile(r"\s+/\s+")
SOURCE_CUT_RE = re.compile(r"\s*(?:\[\s*(?:\.\.\.|…)\s*\]|\.\.\.|…)\s*")
QUOTE_MARKS = "\\\"'`*_\u2018\u2019\u201a\u201b\u201c\u201d\u201e\u00ab\u00bb"  # dropped, escaped or not
DASHES = "\u2010\u2011\u2012\u2013\u2014\u2015\u2212"  # hyphens, en and em dashes, minus: read as "-"
DASH_RE = re.compile(r" *-[- ]*")  # " - ", "--" and a dash with spaces: one "-"
QUOTE_OPEN_RE = re.compile(r"""^(\s*(?:-\s+)?)(quote\s*:\s*)(["'])(.*)$""")
# A GM decision or ruling, written down: a source, never play. The plural is the live logs' own form ("GM DECISIONS /
# RULINGS (FLAG ...): ..."). The brief lists such lines (_brief_decision), never a STATE line or a source record.
DECISION_RE = re.compile(r"\bGM (?:DECISIONS?|RULINGS?)\b|\bRULINGS?\b")
# _history's rule from before the plural, kept as it was: any line naming one in the singular is no play, a STATE
# line or source record too, so a quote of the governing STATE line (which names the rulings in force) is via:
# brief. A STATE line naming them only in the plural stays play; the live logs hold none.
SINGULAR_DECISION_RE = re.compile(r"\bGM (?:DECISION|RULING)\b|\bRULING\b")
NOT_A_SOURCE = {"struck": "only in a struck draft, which is never a source",
                "state": "only in the session frontmatter, the current state being written, not a source"}


def _norm_lines(text: str) -> list[str]:
    """Each line of a text as the quote check compares it, normalised in one pass over the whole text
    (str.replace, not str.translate, which is several times slower on a long non-ASCII log)."""
    text = unicodedata.normalize("NFKC", text).replace("\t", " ")
    for mark in QUOTE_MARKS:
        text = text.replace(mark, "")
    for dash in DASHES:
        text = text.replace(dash, "-")
    return [" ".join(line.split()) for line in DASH_RE.sub("-", text).casefold().splitlines()]


def _norm_quote(text: str) -> str:
    return " ".join(_norm_lines(text))


def _quote_groups(quote: str) -> list[list[tuple[str, str]]]:
    """Each excerpt of a quote, as its pieces between cuts: (as written, normalised). A piece with fewer
    than two letters or digits ("|", "--", "a") would be found anywhere, so it is no quote."""
    groups = []
    for excerpt in SOURCE_GROUP_RE.split(quote):
        pieces = ((piece.strip(), _norm_quote(piece).strip(" .,;:!?-")) for piece in SOURCE_CUT_RE.split(excerpt))
        pieces = [(raw, norm) for raw, norm in pieces if sum(ch.isalnum() for ch in norm) >= 2]
        if pieces:
            groups.append(pieces)
    return groups


def _closes(rest: str, mark: str) -> bool:
    """rest holds the closing mark of a quoted scalar."""
    if mark == '"':
        return re.search(r'(?<!\\)(?:\\\\)*"', rest) is not None
    return "'" in rest.replace("''", "")


def _fold_quotes(text: str) -> str:
    """A quoted quote: value that runs over lines, as one line, so the whole quote is checked."""
    lines, out, i = text.splitlines(), [], 0
    while i < len(lines):
        line, m = lines[i], QUOTE_OPEN_RE.match(lines[i])
        if m and not _closes(m.group(4), m.group(3)):
            col = len(m.group(1))  # a continuation is indented past its key
            while i + 1 < len(lines) and lines[i + 1].strip() and _indent(lines[i + 1]) > col:
                i += 1
                line += " " + lines[i].strip()
                if _closes(line[m.end(3):], m.group(3)):
                    break
        out.append(line)
        i += 1
    return "\n".join(out)


def _is_file(path: Path) -> bool:
    try:
        return path.is_file()
    except (OSError, ValueError):  # a name too long, a NUL byte
        return False


def _source_text(path: Path, cache: dict):
    """(normalised text, line starts, zones, lines) of a source file, or None when it cannot be read.

    zones marks each line body, struck or state (a log's frontmatter); lines is
    None for a file that is not a log, so nothing in it is history.
    """
    if path not in cache:
        try:
            if not _is_file(path) or path.stat().st_size > SOURCE_FILE_MAX:
                raise OSError
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except (OSError, ValueError):
            cache[path] = None
            return None
        zones, log = ["body"] * len(lines), _is_log(path)
        for start, end in struck_spans(lines):
            zones[start:end] = ["struck"] * (end - start)
        fm_end = _frontmatter_end(lines) if log else None
        if fm_end is not None:
            zones[:fm_end + 1] = ["state"] * (fm_end + 1)
        parts = _norm_lines("\n".join(lines))
        if len(parts) != len(lines):  # normalising made or lost a line break: go line by line
            parts = [_norm_quote(line) for line in lines]
        starts, at = [], 0
        for part in parts:
            starts.append(at)
            at += len(part) + 1
        cache[path] = (" ".join(parts), starts, zones, lines if log else None)
    return cache[path]


def _history(lines, i: int):
    """The beat whose prose or record line i of a log is in, or None (a Day Plan, a header, a beat's working,
    a GM decision or ruling, a file that is not a log). A line naming one only in the plural is a decision as the
    brief lists it: never a STATE line or a source record, which stay play."""
    if lines is None or SINGULAR_DECISION_RE.search(lines[i]) or _brief_decision(lines[i]):
        return None
    for j in range(i, -1, -1):
        if HEADING_RE.match(lines[j]):
            info = parse_beat_heading(lines[j])
            return info["beat"] if info else None
    return None


def _find_excerpt(pieces, text: str, starts: list, zones: list):
    """(line index, None) where an excerpt's pieces are, in order and close; else (None, the zone that held it)."""
    first, blocked = pieces[0][1], None
    at = text.find(first)
    while at != -1:
        line = bisect.bisect_right(starts, at) - 1
        if zones[line] != "body":
            blocked = blocked or zones[line]
        else:
            end = at + len(first)
            for _raw, piece in pieces[1:]:
                nxt = text.find(piece, end)
                if nxt == -1 or nxt - end > SOURCE_CUT_MAX or zones[bisect.bisect_right(starts, nxt) - 1] != "body":
                    break
                end = nxt + len(piece)
            else:
                return line, None
        at = text.find(first, at + 1)
    return None, blocked


def _files_named(root: str, name: str, index: dict) -> list[Path]:
    """Files under the project with this base name (dot directories skipped), built once per check."""
    if not index:
        for top, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for file in files:
                index.setdefault(file.casefold(), []).append(Path(top) / file)
        index.setdefault("", [])
    return index.get(name.casefold(), [])


def _source_paths(field: str, root: str, session_dir, index: dict) -> list[Path]:
    """The files a sourced entry's file field names, inside the project: the whole field as one path
    (a name may hold commas or parentheses), then each part between commas and semicolons. Each is
    read up to its suffix, so a line number after it ("day-0199.md l.126", "day-0199.md:126") is dropped."""
    parts = [field] + re.split(r"[;,]", re.sub(r"\([^()]*\)", " ", field))
    found = []
    for part in parts:
        m = SOURCE_SUFFIX_RE.match(part.strip().strip("`\"' "))
        if not m:
            continue
        raw = os.path.expanduser(m.group(1).strip())
        bases = [""] if os.path.isabs(raw) else [root] + ([str(session_dir)] if session_dir else [])
        hits = [p for p in (_inside_project(Path(root), os.path.join(base, raw), suffixes=None) for base in bases)
                if p is not None and _is_file(p)]
        if not hits and "/" not in raw and not os.path.isabs(raw):
            hits = _files_named(root, raw, index)
        found += [h for h in hits if h not in found]
    return found


def _not_a_source(root: str, path: Path) -> bool:
    """The gate's own scratch (any dot directory) and its log."""
    rel = os.path.relpath(path, root)
    return rel == os.path.normpath(VALIDATOR_LOG) or any(part.startswith(".") for part in Path(rel).parts[:-1])


def _brief_gave(brief: dict, root: str, path: Path, line: int, source, pieces) -> bool:
    """Whether the brief gave the GM this excerpt from its file: it carries the line the excerpt is on whole,
    or a keyed entry's verified quote of that file and line holds every piece. A decision line the brief cut
    to its head, a NOT FOUND keyed quote and the Keeper's input carry nothing. The line found first may be one the
    brief leaves out (an earlier STATE line) while it carries, as one of its own lines, another body line of that
    file, not play, that holds the excerpt in order (the governing STATE line): that line gave it."""
    text, starts, zones, lines = source

    def whole(k):
        end = starts[k + 1] - 1 if k + 1 < len(starts) else len(text)
        return text[starts[k]:end].strip()
    if all(norm in brief["text"] for _raw, norm in pieces):  # every piece too: one past a cut may sit on the next line
        if whole(line) and whole(line) in brief["text"]:
            return True
        for k in range(len(starts)):
            if k == line or zones[k] != "body":
                continue
            other = whole(k)
            if (other and other in brief.get("lines", ()) and _find_excerpt(pieces, other, [0], ["body"])[0] == 0
                    and _history(lines, k) is None):
                return True
    quotes = brief["keyed"].get((_relative(Path(root), path), line + 1), [])
    return any(all(norm in quote for _raw, norm in pieces) for quote in quotes)


def source_quotes(turn: dict, project, session_dir=None, entries=None, brief=None) -> dict:
    """{"checked", "verified", "quotes"}: each SOURCE_LOG sourced entry (or each of entries), found in its file
    or not. brief (from _brief_for) marks each verified quote that is not history via: brief when the brief
    gave the GM its line from that file (see _brief_gave), else opened: its words were not carried, so the GM
    opened the file, or recalled them."""
    root = os.path.abspath(project)
    if entries is None:
        data = parse_yaml(_fold_quotes(turn.get("SOURCE_LOG", "") or ""))
        entries = data.get("sourced") if isinstance(data, dict) else None
        entries = entries if isinstance(entries, list) else []
    cache, index, quotes = {}, {}, []
    keeper = _norm_quote(turn.get("KEEPER_INPUT", "") or "")
    for n, entry in enumerate(entries, 1):
        entry = entry if isinstance(entry, dict) else {}
        field, quote = str(entry.get("file") or "").strip(), entry.get("quote")
        result = {"n": n, "claim": str(entry.get("claim") or "")[:80], "file": field or None, "line": None,
                  "verified": False, "history": None, "reason": None}
        quotes.append(result)
        groups = _quote_groups(quote) if isinstance(quote, str) else []
        if not entry or not groups:
            result["reason"] = "not a claim, file and quote" if not entry else "no quote"
            continue
        if re.search(r"\bKEEPER_INPUT\b", field):
            texts = [(None, (keeper, [0], ["body"], None))]
            result["file"] = "KEEPER_INPUT"
        else:
            paths = _source_paths(field, root, session_dir, index)
            scratch = [p for p in paths if _not_a_source(root, p)]
            paths = [p for p in paths if p not in scratch]
            texts = [(path, _source_text(path, cache)) for path in paths]
            texts = [(path, text) for path, text in texts if text]
            if not texts:
                result["reason"] = (f"{_relative(Path(root), scratch[0])} is the gate's scratch or log, not a source"
                                    if scratch else f"cannot read {_relative(Path(root), paths[0])}" if paths
                                    else f"no file found for {field[:120]!r}")
                continue
            result["file"] = _relative(Path(root), texts[0][0])
        hits, blocked = [], set()
        for pieces in groups:
            hit = None
            for path, (text, starts, zones, lines) in texts:
                line, zone = _find_excerpt(pieces, text, starts, zones)
                if line is not None:
                    hit = (path, line, _history(lines, line))
                    break
                blocked.add(zone)
            hits.append(hit)
        if all(hits):
            path, line, beat = hits[0]
            result.update(verified=True, history=beat, line=line + 1 if path else None,
                          file=_relative(Path(root), path) if path else result["file"])
            if brief is not None and path is not None and beat is None:
                sources = dict(texts)
                result["via"] = "brief" if all(_brief_gave(brief, root, h[0], h[1], sources[h[0]], g)
                                               for h, g in zip(hits, groups)) else "opened"
            continue
        pieces = groups[hits.index(None)]
        named = result["file"] if len(texts) == 1 else "the files named (%s)" % ", ".join(
            _relative(Path(root), p) for p, _t in texts if p)
        zone = next((z for z in ("struck", "state") if z in blocked), None)
        if zone:
            result["reason"] = f'"{pieces[0][0][:120]}" is {NOT_A_SOURCE[zone]}'
        elif len(pieces) > 1 and all(any(norm in t[0] for _p, t in texts) for _raw, norm in pieces):
            result["reason"] = (f'the pieces of "{" ... ".join(raw for raw, _n in pieces)[:120]}" are in {named} '
                                f"but not in order and close together")
        else:
            missing = next(raw for raw, norm in pieces if not any(norm in t[0] for _p, t in texts))
            result["reason"] = f'not in {named}: "{missing[:120]}"'
    result = {"checked": len(quotes), "verified": sum(q["verified"] for q in quotes), "quotes": quotes}
    if brief is not None:
        result["opened"] = sum(q.get("via") == "opened" for q in quotes)
    return result


# ---------------------------------------------------------------- GM brief
# What the GM starts each beat from, built in code from the files on disk (no
# model call), within a size budget: the current state (frontmatter, header, the
# run state's current entry, the governing STATE line), the day's Day Plans, the
# findings carried from a soft background review, the entries of the project's
# keyed table whose keys match the Keeper's input or the last few beats (each
# with its source and its quotes found in that source, so the GM cites rather
# than searches), and the recent beats under the continuity rule, newest kept
# first. The report says what was carried, what was dropped for the budget, and
# the size. A keyed table is the SillyTavern lorebook idea, owned by the project:
# the canon register's keyed_entries row, else keyed-entries.md beside the
# campaign's log directory.

BRIEF_FILE = "Work/rp/.gate/brief.md"
BRIEF_REPORT = "Work/rp/.gate/brief.json"
BRIEF_MIN, BRIEF_MAX = 8_000, 1_000_000
KEYED_DEFAULT = "keyed-entries.md"
PRICED_DEFAULT = "Lore/TTRPG/canon-sources.md"
PRICED_FRAGMENT = PRICED_RULE.parent / "canon-priced-stakes.md"
SCAN_BEATS = 3  # besides the Keeper's input, keys are matched in the last few beats
WHOLE_BEATS = 1  # the last beats every brief carries, whatever the budget; the two before them go in after the keyed
DECISION_LINE_MAX = 400  # bytes of a GM DECISION or RULING line from a beat not carried; the rest is a read away
# One Read of the brief on Claude Code is refused past 25,000 tokens, and one with no limit returns only a first
# part: a 79,098-byte live brief (B4355) was 33,091 tokens (2.4 bytes a token). The report's reads cut it
# into parts this size or less (even at 1.6 bytes a token), which the GM issues in one message.
BRIEF_READ_BYTES = 40_000
STATE_LINE_RE = re.compile(r"^\s*(?:[-*>]\s*)?\**STATE\**\s*:")  # "STATE: ~07:35 D199 ...", capitals only
# A source record ("SOURCED: ...", "SOURCED (priced stake: ...):", "SOURCE_LOG ...") cites the rulings it names, even
# with their labels ("l.436 GM RULING: the collar answers ..."), so it is no decision line; nor is a STATE line, which
# the governing one supersedes.
SOURCE_RECORD_RE = re.compile(r"^\s*(?:[-*>]\s*)?\**SOURCE(?:D|_LOG)\b")
BRIEF_DECISION_ITEM_RE = re.compile(r"^- Beat \S+, .+? line \d+: (.*)$")  # a line of the brief's decision list
# "## Resume point (history: B4099 ...)", "(history; superseded ...)", "(superseded by ...)"; never the level-1 title
HISTORY_HEADING_RE = re.compile(r"\(\s*(?:history\s*[:;,]|superseded\b)", re.I)
KEYED_HEADING_RE = re.compile(r"^##\s+(.+?)\s*#*\s*$")
KEYED_FIELD_RE = re.compile(r"^\s*(?:[-*]\s+)?(keys|source|quote|note|always)\s*:\s*(.*?)\s*$", re.I)
KEY_SPLIT_RE = re.compile(r"\s*(/(?:[^/\\]|\\.)+/[a-z]*|[^,]+?)\s*(?:,|$)")
REGEX_KEY_RE = re.compile(r"^/(.+)/([a-z]*)$", re.S)  # /regex/ or /regex/i: always case-insensitive


def _register_paths(project: Path, name: str) -> list[str]:
    """A source register's paths in the canon register: a table row (| `name` | `path` |), or the backticked
    path opening a '## name' section."""
    try:
        text = (project / CANON_LAYOUT).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    m = re.search(r"(?m)^\|\s*`?%s`?\s*\|([^|\n]+)\|" % re.escape(name), text)
    if m:
        return re.findall(r"`([^`]+)`", m.group(1))
    m = re.search(r"(?ms)^#{2,6}\s+`?%s`?\s*$(.*?)(?=^#{1,6}\s|\Z)" % re.escape(name), text)
    first = next((part for part in re.split(r"\n\s*\n", m.group(1)) if part.strip()), "") if m else ""
    return re.findall(r"`([^`]+)`", first)[:1]


def keyed_tables(project, data: dict, warnings=None) -> list[str]:
    """The project's keyed tables: the canon register's keyed_entries globs, else <campaign>/keyed-entries.md
    beside the session's log_dir (Work/rp/<campaign>/log). A register row that finds no table is a warning."""
    project = Path(project)
    patterns = _register_paths(project, "keyed_entries")
    registered = bool(patterns)
    if not patterns:
        log_dir, _warning = _log_dir(project, data)
        if log_dir is None:
            return []
        base = Path(log_dir).parent if Path(log_dir).name == "log" else Path(log_dir)
        patterns = [(base / KEYED_DEFAULT).as_posix()]
    found = []
    for pattern in patterns:
        try:
            matches = sorted(glob.glob(os.path.join(project, pattern), recursive=True))
        except (ValueError, OSError):
            continue
        for match in matches:
            path = _inside_project(project, os.path.relpath(match, project))
            if path is not None and _is_file(path) and _relative(project, path) not in found:
                found.append(_relative(project, path))
    if registered and not found and warnings is not None:
        warnings.append(f"The canon register's keyed_entries row ({', '.join(patterns)}) finds no file, so no keyed "
                        "table is read (the default beside log_dir is not tried while the row stands).")
    return found


def _unquote(value: str) -> str:
    value = value.strip()
    pairs = {'"': '"', "'": "'", "“": "”", "`": "`"}
    if len(value) >= 2 and value[0] in pairs and value[-1] == pairs[value[0]]:
        return value[1:-1]
    return value


def _key_pattern(key: str):
    """A key as a case-insensitive pattern: /regex/ (flags after it are ignored), or words matched whole, quoted or
    not (a trailing * takes any ending). A word key with no letter or digit would match anything: refused."""
    m = REGEX_KEY_RE.match(key)
    if m:
        return re.compile(m.group(1), re.I)
    key = _unquote(key)
    if not any(ch.isalnum() for ch in key):
        raise re.error("it has no letter or digit, so it would match any text")
    words = key.split()
    if not words:
        return None
    body = r"\s+".join(re.escape(w.rstrip("*")) + (r"\w*" if w.endswith("*") else "") for w in words)
    return re.compile(r"(?<!\w)%s(?!\w)" % body, re.I)


def parse_keyed(text: str, table: str) -> tuple[list[dict], list[str]]:
    """The entries of a keyed table: '## <id>' then '- keys:', '- source:', '- quote:' (repeatable),
    '- note:' and '- always: true' lines. Fenced lines are skipped."""
    lines, entries, warnings, current = text.splitlines(), [], [], None
    fenced = _fenced(lines)
    for i, line in enumerate(lines):
        if i in fenced:
            continue
        m = KEYED_HEADING_RE.match(line)
        if m:
            current = {"id": m.group(1).strip(), "table": table, "line": i + 1, "keys": [], "source": None,
                       "quotes": [], "note": None, "always": False}
            entries.append(current)
            continue
        f = KEYED_FIELD_RE.match(line) if current is not None else None
        if not f:
            continue
        field, value = f.group(1).lower(), f.group(2)
        if field == "keys":
            current["keys"] += [k for k in KEY_SPLIT_RE.findall(value) if k]
        elif field == "source":
            current["source"] = value.strip("`\"' ")
        elif field == "quote":
            current["quotes"].append(_unquote(value))
        elif field == "note":
            current["note"] = value
        else:
            current["always"] = value.strip().lower() in ("true", "yes")
    for entry in entries:
        entry["patterns"] = []
        for key in entry["keys"]:
            try:
                pattern = _key_pattern(key)
            except re.error as exc:
                warnings.append(f"{table} '{entry['id']}': key {key!r} is not a valid pattern ({exc}); skipped.")
                continue
            if pattern is not None:
                entry["patterns"].append((key, pattern))
        if not entry["patterns"] and not entry["always"]:
            warnings.append(f"{table} '{entry['id']}' has no keys and is not always: it never matches.")
        if entry["quotes"] and not entry["source"]:
            warnings.append(f"{table} '{entry['id']}' quotes without a source: its quotes are not checked.")
    return entries, warnings


def _priced_entry(project: Path, keeper: str):
    """The priced-stakes rule as a keyed entry (the prompt hook's nudge, on every harness), when the input
    matches it."""
    priced = _priced_regex()
    if not (priced and keeper and priced.search(keeper)):
        return None
    try:
        note = PRICED_FRAGMENT.read_text(encoding="utf-8")
    except OSError:
        note = "This turn touches a priced stake: open the governing source before adjudicating it."
    note = re.sub(r"</?system-reminder>", "", note).strip()
    register = (_register_paths(project, "priced_stakes") or [PRICED_DEFAULT])[0]
    return {"id": "Priced stake", "table": "asha", "keys": ["priced-stakes rule"], "source": register, "quotes": [],
            "note": note, "always": False, "patterns": [], "priced": True}


def _ranges_lines(entry: dict) -> list[int]:
    return [n for a, b in entry.get("parts") or [[entry["start"], entry["end"]]] for n in range(a, b + 1)]


def _brief_beats(project: Path, cont, skip: set, cache: dict) -> list[dict]:
    """The beats of the continuity read, oldest first, each with its text, its lines ((number, line)), its STATE
    lines ((number, line)) and its decision lines: struck drafts and the day's Day Plans (skip, by (path, line))
    left out."""
    beats = []
    entries = list((cont or {}).get("previous") or []) + ([cont["current"]] if (cont or {}).get("current") else [])
    for entry in entries:
        if entry["path"] not in cache:
            cache[entry["path"]] = _read_lines(project / entry["path"]) or []
        lines, allowed = cache[entry["path"]], set(_ranges_lines(entry))
        for unit in _beat_units(lines, entry["start"] - 1, entry["end"]):
            numbers = [n for n in range(unit["start"], unit["end"] + 1)
                       if n in allowed and (entry["path"], n) not in skip]
            while numbers and not lines[numbers[-1] - 1].strip():
                numbers.pop()
            if numbers:
                states = [(n, lines[n - 1].strip()) for n in numbers if STATE_LINE_RE.match(lines[n - 1])]
                beats.append({"beat": unit["beat"], "path": entry["path"], "start": numbers[0], "end": numbers[-1],
                              "text": "\n".join(lines[n - 1] for n in numbers).strip("\n"),
                              "lines": [(n, lines[n - 1]) for n in numbers], "states": states,
                              "decisions": [(n, lines[n - 1].strip()) for n in numbers
                                            if _brief_decision(lines[n - 1])]})
    return beats


def _brief_decision(line: str) -> bool:
    """A GM DECISION or RULING line, as the brief lists them for a beat it does not carry. Never a STATE line (the
    governing one supersedes it, and the logs carry a ruling in force forward in each) or a source record (it cites
    rulings whose own lines are the decisions)."""
    return bool(DECISION_RE.search(line) and not HEADING_RE.match(line) and not STATE_LINE_RE.match(line)
                and not SOURCE_RECORD_RE.match(line))


def _governing_state(beats: list[dict]):
    """The last STATE line in the read, newest beat first: the line that governs current state."""
    for beat in reversed(beats):
        if beat["states"]:
            line, text = beat["states"][-1]
            return {"path": beat["path"], "beat": beat["beat"], "line": line, "text": text}
    return None


def _header_text(body: list[str], numbers: list[int]) -> tuple:
    """(text, sections): the header's lines, each section its own heading marks history ("(history ..." or
    "(superseded ...") cut to its heading and a pointer to its lines, and those sections as {heading, lines}.
    A section runs to the next heading of its level or above."""
    fenced = _fenced([body[n - 1] for n in numbers])
    out, sections, i = [], [], 0
    while i < len(numbers):
        line = body[numbers[i] - 1]
        m = HEADING_RE.match(line) if i not in fenced else None
        if not (m and len(m.group(1)) > 1 and HISTORY_HEADING_RE.search(m.group(2))):
            out.append(line)
            i += 1
            continue
        j = i + 1
        while j < len(numbers):
            h = HEADING_RE.match(body[numbers[j] - 1]) if j not in fenced else None
            if h and len(h.group(1)) <= len(m.group(1)):
                break
            j += 1
        inner = [n for n in numbers[i + 1:j] if body[n - 1].strip()]
        out.append(line)
        if inner:
            span = f"{inner[0]}-{inner[-1]}"
            sections.append({"heading": line.strip(), "lines": span})
            out += ["", f"(History by its own heading: lines {span} are a read away, not carried.)", ""]
        i = j
    return "\n".join(out).strip("\n"), sections


def brief_reads(text: str, limit: int = BRIEF_READ_BYTES) -> list[dict]:
    """The brief as Read calls ({offset, limit}, by line), each part at most limit bytes (a longer line alone).
    Lines are counted by newline, as a Read counts them: a form feed or a line separator ends no line."""
    reads, offset, count, size = [], 1, 0, 0
    for line in text.split("\n")[:-1] if text.endswith("\n") else text.split("\n"):
        cost = len(line.encode("utf-8")) + 1
        if count and size + cost > limit:
            reads.append({"offset": offset, "limit": count})
            offset, count, size = offset + count, 0, 0
        count, size = count + 1, size + cost
    if count:
        reads.append({"offset": offset, "limit": count})
    return reads


def keeper_digest(text: str) -> str:
    return hashlib.sha256(" ".join((text or "").split()).encode("utf-8")).hexdigest()[:16]


def build_brief(project, session_file, keeper_input: str, *, config=None, budget=None, beat=None,
                source_dir=None) -> tuple:
    """(text, report): the GM brief for one turn, built from disk within budget bytes. source_dir is where a
    keyed quote's bare file name resolves (default: beside the session file)."""
    project = Path(project)
    config = config or load_config(project)
    budget = budget or config["briefBytes"]
    rel = _relative(project, project / session_file)
    lines = _read_lines(project / rel)
    if lines is None:
        raise ValueError(f"{rel} cannot be read")
    data = read_frontmatter(project / rel)
    state = live_state(project, rel)
    cont = continuity(project, rel, config["log"]["previousBeats"])
    cache, warnings = {rel: lines}, []
    report = {"path": BRIEF_FILE, "session": rel, "budget": budget, "keeper_input": keeper_digest(keeper_input),
              "warnings": warnings}
    held = provisional_keys(lines)
    if held:
        warnings.append(f"{rel} holds a provisional beat ({held[0]}) whose review has not settled: collect it "
                        "(Step 1b) and build the brief again.")
    report["provisional"] = held

    def text_of(entry) -> str:
        if entry["path"] not in cache:
            cache[entry["path"]] = _read_lines(project / entry["path"]) or []
        body = cache[entry["path"]]
        return "\n".join(body[n - 1] for n in _ranges_lines(entry) if 0 < n <= len(body)).strip("\n")

    day = (cont or {}).get("day")
    plans = _day_span(project, rel, day)["plans"] if day is not None else []
    skip = {(e["path"], n) for e in plans for n in _ranges_lines(e)}
    beats = _brief_beats(project, cont, skip, cache)
    numbers = [n for b in beats if (n := _beat_number(b["beat"])) is not None]
    next_beat = beat or (str(max(numbers) + 1) if numbers else None)
    governing = _governing_state(beats)

    head = ["=== GM brief ===", "",
            f"Built by rp_gate.py brief from the files on disk (no model call), within {budget} bytes. It carries "
            "what this beat starts from. Cite the file each fact comes from (every section names it), never this "
            "brief. When the beat rests on something it does not carry, open the source; what it left out is listed "
            "at the end. Earlier STATE lines are left out (the governing STATE line supersedes them), and a header "
            "section its own heading marks history is a pointer to its lines.", "", f"SESSION_FILE: {rel}"]
    if next_beat:
        head.append(f"NEXT_BEAT: {next_beat}")
    head += ["KEEPER_INPUT:", keeper_input.strip(), "", live_state_block(state, cont, rel)]
    report["live_state"] = head[-1]
    must = ["\n".join(head)]
    current = ["=== Current state ==="]
    if state.get("frontmatter"):
        fm = state["frontmatter"]
        current += ["", f"== Frontmatter ({rel} lines {fm['start']}-{fm['end']}) ==",
                    text_of({"path": rel, **fm})]
    report["header_history"] = []
    if state.get("header"):
        numbers = [n for n in _ranges_lines(state["header"]) if 0 < n <= len(lines)]
        header, report["header_history"] = _header_text(lines, numbers)
        current += ["", f"== Header ({rel} lines {_ranges_text(state['header'])}) ==", header]
    run_state = state.get("run_state")
    if run_state and run_state.get("current") and not run_state.get("generated"):
        cur = run_state["current"]
        current += ["", f"== Run state, current entry ({run_state['path']} lines {cur['start']}-{cur['end']}) ==",
                    text_of({"path": run_state["path"], **cur})]
    if governing:
        report["state_line"] = {k: governing[k] for k in ("path", "beat")}
        current += ["", f"== Governing STATE line (Beat {governing['beat']}, {governing['path']}) ==", governing["text"]]
    else:
        report["state_line"] = None
    must.append("\n".join(current))
    report["plans"] = [{"path": e["path"], "lines": _ranges_text(e), "heading": e.get("heading")} for e in plans]
    for e in plans:
        must.append(f"=== Day Plan ({e['path']} lines {_ranges_text(e)}) ===\n{text_of(e)}")
    if not plans:
        warnings.append("No Day Plan was found for the current day.")
    carried = carried_path(project, rel)
    report["carried_findings"] = carried.is_file()
    if report["carried_findings"]:
        must.append("=== Carried findings ===\n" + carried.read_text(encoding="utf-8").strip())

    # Keyed entries: the priced-stakes rule first, then the table's matches, best first.
    tables = keyed_tables(project, data, warnings)
    entries = []
    for table in tables:
        found, table_warnings = parse_keyed((project / table).read_text(encoding="utf-8", errors="replace"), table)
        entries += found
        warnings.extend(table_warnings)
    scan = "\n".join(b["text"] for b in beats[-SCAN_BEATS:])
    ranked = []
    for order, entry in enumerate(entries):
        in_input = [k for k, pattern in entry["patterns"] if pattern.search(keeper_input)]
        in_beats = [k for k, pattern in entry["patterns"] if k not in in_input and pattern.search(scan)]
        if entry["always"] or in_input or in_beats:
            ranked.append(((entry["always"], 2 * len(in_input) + len(in_beats), -order), entry, in_input, in_beats))
    ranked.sort(key=lambda r: r[0], reverse=True)
    priced = _priced_entry(project, keeper_input)
    report["priced_input"] = priced is not None
    if priced:
        ranked.insert(0, ((True, 0, 0), priced, ["priced-stakes rule"], []))
    checks = source_quotes({}, project, Path(source_dir) if source_dir else project / Path(rel).parent, entries=[
        {"claim": f"{entry['id']} #{i}", "file": entry["source"], "quote": quote}
        for _score, entry, _i, _b in ranked if entry["source"] for i, quote in enumerate(entry["quotes"], 1)])
    results = iter(checks["quotes"])
    keyed = []
    for _score, entry, in_input, in_beats in ranked:
        out = [f"== {entry['id']} =="]
        matched = ([f"{', '.join(in_input)} (the Keeper's input)"] if in_input else []) + (
            [f"{', '.join(in_beats)} (the last beats)"] if in_beats else [])
        out.append("matched: " + ("; ".join(matched) if matched else "always"))
        if entry["source"]:
            out.append(f"source: {entry['source']}")
        quotes = []
        for quote in entry["quotes"] if entry["source"] else []:
            result = next(results)
            quotes.append({"verified": result["verified"], "line": result["line"]})
            where = (f"verified, {result['file']} line {result['line']}" if result["verified"]
                     else f"NOT FOUND: {result['reason']}; open the source before relying on it")
            out.append(f"quote ({where}): \"{quote}\"")
        if entry["note"]:
            out.append(f"note (from the table, not a source): {entry['note']}")
        keyed.append({"id": entry["id"], "table": entry["table"], "source": entry["source"], "in_input": in_input,
                      "in_beats": in_beats, "quotes": quotes, "text": "\n".join(out),
                      "priced": bool(entry.get("priced"))})
    report["keyed"] = {"tables": tables, "entries": len(entries), "matched": [], "dropped": [],
                       "unverified": [k["id"] for k in keyed if any(not q["verified"] for q in k["quotes"])]}

    # The budget: what must go in (the last beat, and the GM decisions and rulings of beats not carried), then the
    # keyed entries, best first, then the beats before the last, newest first; then exact, dropping the last
    # optional piece added until the whole brief fits. A carried beat leaves its STATE lines out: the governing
    # one is under Current state, and it supersedes the rest.
    always = beats[-WHOLE_BEATS:]
    rest = beats[:-WHOLE_BEATS] if len(beats) > WHOLE_BEATS else []

    def beat_text(b):
        left = {n for n, _line in b["states"]}
        notes = [f"STATE line {n} " + ("is the governing STATE line, under Current state"
                                       if governing and (b["path"], n) == (governing["path"], governing["line"])
                                       else "left out: superseded by the governing STATE line")
                 for n, _line in b["states"]]
        body = "\n".join(line for n, line in b["lines"] if n not in left).strip("\n")
        tail = f"; {'; '.join(notes)}" if notes else ""
        return f"<!-- Beat {b['beat']}: {b['path']} lines {b['start']}-{b['end']}{tail} -->\n{body}"
    decisions = {id(b): b["decisions"] for b in rest}

    def decision_line(b, item):
        n, text = item
        head = text.encode("utf-8")
        if len(head) > DECISION_LINE_MAX:  # its head says what it decides; the line is a read away
            text = head[:DECISION_LINE_MAX].decode("utf-8", "ignore").rstrip() + f" ... (cut: read line {n} whole)"
        return f"- Beat {b['beat']}, {b['path']} line {n}: {text}"
    cost = lambda t: len(t.encode("utf-8")) + 2  # noqa: E731
    chosen = [k for k in keyed if k["priced"]]
    carried_beats = list(always)

    def assemble():
        parts = list(must)
        dropped = [b for b in beats if b not in carried_beats]
        held = [decision_line(b, x) for b in dropped for x in decisions.get(id(b), [])]
        if held:
            parts.append("=== GM decisions and rulings in beats not carried ===\n" + "\n".join(held))
        if keyed:
            ids = {id(k) for k in chosen}
            parts.append(f"=== Keyed entries ({len(chosen)} of {len(entries)} in {', '.join(tables) or 'no table'}"
                         f"{'; the priced-stakes rule' if priced else ''}) ===\n" + "\n\n".join(
                             k["text"] for k in keyed if id(k) in ids))
        parts.append(f"=== Recent beats ({len(carried_beats)} of the {len(beats)} the continuity read holds, oldest "
                     "first) ===\n" + "\n\n".join(beat_text(b) for b in carried_beats))
        left = [f"- Beat {b['beat']}: {b['path']} lines {b['start']}-{b['end']}" for b in dropped]
        left += [f"- keyed entry {k['id']}" for k in keyed if k not in chosen]
        parts.append("=== Not carried ===\n" + ("\n".join(left) if left else
                                                 "- nothing in the continuity read was left out"))
        return "\n\n".join(parts) + "\n", dropped

    used = len(assemble()[0].encode("utf-8"))  # the must content, with every optional beat listed as left out
    added = []  # optional pieces, in the order they went in, for the exact pass
    for k in keyed:
        if k not in chosen and used + cost(k["text"]) <= budget:
            chosen.append(k)
            added.append(("keyed", k))
            used += cost(k["text"])
    for b in reversed(rest):
        extra = cost(beat_text(b)) - sum(cost(decision_line(b, x)) for x in decisions[id(b)])
        if used + extra > budget:
            break
        carried_beats.insert(0, b)
        added.append(("beat", b))
        used += extra
    text, dropped = assemble()
    while len(text.encode("utf-8")) > budget and added:
        kind, piece = added.pop()
        (chosen if kind == "keyed" else carried_beats).remove(piece)
        text, dropped = assemble()
    report["keyed"]["matched"] = [{key: k[key] for key in ("id", "table", "source", "in_input", "in_beats", "quotes")}
                                  for k in keyed if k in chosen]
    report["keyed"]["dropped"] = [k["id"] for k in keyed if k not in chosen]
    report.update(bytes=len(text.encode("utf-8")), over_budget=len(text.encode("utf-8")) > budget,
                  reads=brief_reads(text), next_beat=next_beat, shipped=_shipped_of("\n".join(lines[:2001])),
                  decisions=sum(len(decisions.get(id(b), [])) for b in dropped),
                  beats={"carried": [b["beat"] for b in carried_beats],
                         "dropped": [{"beat": b["beat"], "path": b["path"], "lines": f"{b['start']}-{b['end']}"}
                                     for b in dropped]})
    return text, report


def write_brief(project, session_file, keeper_input: str, **kw) -> dict:
    """Build the brief and write it, and its report, under the gate's scratch."""
    project = Path(project)
    text, report = build_brief(project, session_file, keeper_input, **kw)
    path = project / BRIEF_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_atomic(path, text.encode("utf-8"))
    _write_atomic(project / BRIEF_REPORT, json.dumps(report, ensure_ascii=False).encode("utf-8"))
    return report


BRIEF_KEYED_RE = re.compile(r'^quote \(verified, (.+) line (\d+)\): "(.*)"\s*$')


def _brief_for(project: Path, turn: dict):
    """The current brief, when it was built for this turn's input, as {"text": its own text normalised,
    "keyed": {(file, line): [each keyed quote verified there, normalised]}, "lines": {each of its own lines,
    normalised: a decision list item without its "- Beat N, file line M: " head}}, the Keeper's input left out of
    all three (it carries nothing); else None."""
    try:
        report = json.loads((project / BRIEF_REPORT).read_text(encoding="utf-8"))
        text = (project / BRIEF_FILE).read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    if not isinstance(report, dict) or report.get("keeper_input") != keeper_digest(turn.get("KEEPER_INPUT", "")):
        return None
    lines = _read_lines(project / str(report.get("session") or "")) or []
    if _shipped_of("\n".join(lines[:2001])) != report.get("shipped"):
        return None  # built before a later beat shipped: another turn's brief with the same input
    keyed, carried, own, keeper = {}, set(), [], None
    for raw in text.splitlines():
        if keeper is None and raw == "KEEPER_INPUT:":
            keeper = True  # the Keeper's input carries nothing: its lines are not the brief's
        elif keeper and raw.startswith("LIVE_STATE:"):
            keeper = False
        if not keeper:
            own.append(raw)
            m = BRIEF_KEYED_RE.match(raw)
            if m:
                keyed.setdefault((m.group(1), int(m.group(2))), []).append(_norm_quote(m.group(3)))
            item = BRIEF_DECISION_ITEM_RE.match(raw)
            carried.add(_norm_quote(item.group(1) if item else raw).strip())
    return {"text": _norm_quote("\n".join(own)), "keyed": keyed, "lines": carried}


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


def _pc_delta_role(scene: dict, delta: dict):
    """The PC's role in the delta's participants, or None when the delta does not list the PC."""
    pc, listed = scene.get("pc"), delta.get("scene.participants")
    entry = next((p for p in (listed if isinstance(listed, list) else [])
                  if isinstance(p, dict) and pc and same_person(str(p.get("name", "")), pc)), None)
    role = entry.get("role") if entry else None
    return None if role is None or role == "" else str(role)


def _stats(role: str, names) -> dict:
    found = {}
    for name in names:
        m = re.search(r"(?<![\w-])%s\s*[:=]?\s*(-?\d+(?:\s*/\s*\d+)?)(?![\w/])" % re.escape(name), role)
        if m:
            found[name] = re.sub(r"\s+", "", m.group(1))
    return found


def _pc_role_changed(role, scene: dict) -> bool:
    """A real change in the PC's role: a new label, or a stat that moved inside a narrative.

    While the scene's role is a label ("PC", "PC (hooded)", at most ROLE_LABEL_MAX
    characters), any new role is a change. A longer role narrates the scene (the
    time, his last move, who did what) and is rewritten every beat, so only a stat
    it carries counts ("POW 53/60" to "POW 40/60"); anything else rests on
    SOURCE_LOG pc_changed.
    """
    before = str(scene.get("pc_role") or "")
    if role is None or role == before:
        return False
    if len(before) <= ROLE_LABEL_MAX:
        return True
    run = scene.get("run") if isinstance(scene.get("run"), dict) else {}
    names = list(STAT_NAMES) + [str(k) for k in (run.get("stats") or {}) if isinstance(run.get("stats"), dict)]
    old, new = _stats(before, names), _stats(role, names)
    return any(old[name] != new[name] for name in old.keys() & new.keys())


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
    if _declared(source, "pc_changed") or _pc_role_changed(_pc_delta_role(scene, delta), scene):
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
    # A run-state finding the edit pass left never costs a model call: ship drops that entry.
    mechanical = [f for f in edit_findings if not f.get("run")]
    beat = "consequential" if triggers else "routine"
    policy = config["review"]
    if final:
        step = "fix" if mechanical else "ship"
    elif edit_findings and not recheck:
        step = "fix"  # mechanical: the edit pass corrects it, then check --recheck
    elif rewrite or "validate_tag" in triggers:
        step = "review"
    elif policy == "lint-only":
        step = "ship"
    elif policy == "every" or triggers or review_findings or mechanical:
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
    if "briefBytes" in block:
        value = block["briefBytes"]
        if isinstance(value, int) and not isinstance(value, bool) and BRIEF_MIN <= value <= BRIEF_MAX:
            config["briefBytes"] = value
        else:
            warnings.append(f"turnGate.briefBytes {value!r} must be {BRIEF_MIN}-{BRIEF_MAX}.")
    if "background" in block:
        if isinstance(block["background"], bool):
            config["background"] = block["background"]
        else:
            warnings.append(f"turnGate.background {block['background']!r} must be true or false.")
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
    header = lines[:_body_start(lines)]
    hidden = {i for s, e in struck_spans(header) for i in range(s, e)}
    for line in (line for i, line in enumerate(header) if i not in hidden):
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
    the frontmatter but not the log, a delta applied without its beat raises it before any beat.
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


def struck_spans(lines: list[str]) -> list[tuple[int, int]]:
    """[start, end) indices of the struck-draft callouts: a "> [!struck]" line and the lines after it that start
    with ">". A block cut short ends at its last such line, so it never takes in what follows."""
    spans, fenced, i = [], _fenced(lines), 0
    while i < len(lines):
        if i in fenced or not STRUCK_RE.match(lines[i]):
            i += 1
            continue
        j = i + 1
        while j < len(lines) and lines[j].startswith(">") and not STRUCK_RE.match(lines[j]):
            j += 1
        spans.append((i, j))
        i = j
    return spans


def _skip_struck(entry, lines: list[str], spans=None, first: int = 1):
    """A read range (1-based lines) with struck drafts left out: start and end move off any at its edges, and
    parts lists the pieces when one falls inside. None when nothing but struck lines is left. lines[0] is line
    `first` (1 for a whole file)."""
    if not entry:
        return entry
    spans = struck_spans(lines) if spans is None else spans
    inside = [(s + first, e + first - 1) for s, e in spans
              if e + first - 1 >= entry["start"] and s + first <= entry["end"]]
    if not inside:
        return entry
    pieces, lo = [], entry["start"]
    for top, bottom in inside:
        if top > lo:
            pieces.append([lo, top - 1])
        lo = max(lo, bottom + 1)
    if lo <= entry["end"]:
        pieces.append([lo, entry["end"]])
    parts = []
    for a, b in pieces:
        while a <= b and not lines[a - first].strip():
            a += 1
        while b >= a and not lines[b - first].strip():
            b -= 1
        if a <= b:
            parts.append([a, b])
    if not parts:
        return None
    entry["start"], entry["end"] = parts[0][0], parts[-1][1]
    entry.pop("parts", None)
    if len(parts) > 1:
        entry["parts"] = parts
    return entry


def _last_beats(project: Path, rel, beats: int):
    """The last beats of a file, its footer excluded; any END block after them included; struck drafts left out."""
    lines = _read_lines(project / rel) if rel and beats else None
    if not lines:
        return None
    stop = _footer_start(lines)
    units = _beat_units(lines, _body_start(lines), stop)
    if not units:
        return None
    span = _span(rel, units, beats)
    span["end"] = (_range(rel, lines, units[-1]["cut"], stop) or {"end": span["end"]})["end"]
    return _skip_struck(span, lines)


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
    Day Plan heading; a fenced heading neither opens nor ends one. Struck drafts inside a section are left out."""
    fenced, found, struck = _fenced(lines), [], struck_spans(lines)
    for i in range(lo, stop):
        if i in fenced or not DAY_PLAN_RE.match(lines[i]):
            continue
        named = {int(d) for m in FICTION_DAY_RE.finditer(lines[i]) for d in m.groups() if d}
        if day not in named and (named or i < start):
            continue
        end = next((j for j in range(i + 1, stop) if j not in fenced
                    and (DAY_PLAN_RE.match(lines[j]) or BEAT_HEADING_RE.match(lines[j]))), stop)
        section = _skip_struck(_range(rel, lines, i, end), lines, struck)
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
    Every range leaves struck drafts out; when one falls inside, parts lists the pieces.
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
    struck = struck_spans(lines)
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
        current = _skip_struck(_range(rel, lines, today[-beats]["cut"], stop), lines, struck)
        if current:
            current.update(beats=beats, whole_day=False, first_beat=today[-beats]["beat"], last_beat=today[-1]["beat"])
        result["current"] = current
        result["plans"] = _outside(plans, current)
        return result
    current = _skip_struck(_range(rel, lines, start, stop), lines, struck)
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
        previous.append(_skip_struck(span, lines, struck))  # beats are never struck: something is left
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


def _ranges_text(entry: dict) -> str:
    return ", ".join(f"{a}-{b}" for a, b in entry.get("parts") or [[entry["start"], entry["end"]]])


def live_state_block(state: dict, cont, session: str) -> str:
    """The LIVE_STATE block of /rp:turn Step 2, built from a session's live_state and continuity: where the
    reader finds current state, as line ranges; struck drafts left out (a range in parts)."""
    out = ["LIVE_STATE:"]
    fm, header = state.get("frontmatter"), state.get("header")
    if fm and header and not header.get("parts") and header["start"] <= fm["end"] + 1:
        line = f"  session: {session} lines {fm['start']}-{header['end']}"
    elif fm and header:
        line = f"  session: {session} lines {fm['start']}-{fm['end']}, {_ranges_text(header)}"
    elif fm or header:
        line = f"  session: {session} lines {_ranges_text(fm or header)}"
    else:
        line = f"  session: {session}"
    if header and header.get("truncated"):
        line += "  (header cut at 250 lines)"
    out.append(line)
    run_state = state.get("run_state")
    if run_state and not run_state.get("generated"):  # a generated entry copies the frontmatter's run: block
        current = run_state.get("current")
        section = f"lines {run_state['heading_line']}-{run_state['end_line']}"
        out.append(f"  run_state: {run_state['path']} lines {current['start']}-{current['end']}  (the section: "
                   f"{section})" if current else f"  run_state: {run_state['path']} {section}  (the section; "
                   "search it, do not read it whole)")
    for entry in [cont.get("current")] if cont and cont.get("current") else []:
        when = "so far" if entry.get("whole_day") else f"its last {entry.get('beats')} beats"
        span = f"; B{entry['first_beat']}-B{entry['last_beat']}" if entry.get("first_beat") is not None else ""
        out.append(f"  day: {entry['path']} lines {_ranges_text(entry)}  (Day {cont['day']}: {when}{span})")
    for entry in (cont or {}).get("previous") or []:
        span = f", B{entry['first_beat']}-B{entry['last_beat']}" if entry.get("first_beat") is not None else ""
        out.append(f"  earlier: {entry['path']} lines {_ranges_text(entry)}  ({entry.get('beats')} beats{span})")
    for entry in (cont or {}).get("plans") or []:
        out.append(f"  plan: {entry['path']} lines {_ranges_text(entry)}  ({entry.get('heading', '')})")
    return "\n".join(out)


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


MISSING = object()  # a key the new value no longer has


def _untab(line: str) -> str:
    return line.replace("\t", "  ")  # as parse_yaml reads a tab


def _indent(line: str) -> int:
    line = _untab(line)
    return len(line) - len(line.lstrip(" "))


def _is_content(line: str) -> bool:
    text = line.strip()
    return bool(text) and not text.startswith("#")


def _key_of(line: str, indent: int):
    """(key, rest) a raw line sets at exactly this indent, else (None, None)."""
    if _indent(line) != indent or not _is_content(line):
        return None, None
    text = _strip_comment(_untab(line).rstrip("\r\n")).strip()
    return (None, None) if _is_item(text) else _split_key(text)


def _entry_end(lines: list[str], i: int, indent: int, stop: int) -> int:
    """End (exclusive) of the entry at lines[i]: its deeper lines, with the blank or comment lines between
    them (a blank line inside a block does not end it), but not those after its last deeper line."""
    end = i + 1
    for j in range(i + 1, stop):
        if not _is_content(lines[j]):
            continue
        if _indent(lines[j]) > indent or (_indent(lines[j]) == indent and _is_item(_untab(lines[j]).strip())):
            end = j + 1
        else:
            break
    return end


def _set_key(lines: list[str], start: int, stop: int, indent: int, path: tuple, value_at, prefix=()) -> list[str]:
    """Set one key path in the mapping that lines[start:stop] hold at this indent, from value_at(path),
    keeping every other line as written: a block mapping is entered, a leaf (or an inline value) is
    written whole, a key value_at reports MISSING is deleted, a new key goes at the mapping's end."""
    key, here = path[0], prefix + (path[0],)
    found = max((i for i in range(start, stop) if _key_of(lines[i], indent)[0] == key), default=None)  # the
    # parser keeps a duplicate key's last value, so that is the line to set
    if found is None:
        value = value_at(here)
        return lines if value is MISSING else lines[:stop] + emit_yaml(key, value, indent) + lines[stop:]
    end = _entry_end(lines, found, indent, stop)
    if len(path) > 1 and not (_key_of(lines[found], indent)[1] or "").strip():  # a block mapping: go down
        child = next((j for j in range(found + 1, end) if _is_content(lines[j])), None)
        if child is not None and _indent(lines[child]) > indent and not _is_item(_untab(lines[child]).strip()):
            return _set_key(lines, found + 1, end, _indent(lines[child]), path[1:], value_at, here)
    value = value_at(here)
    return lines[:found] + ([] if value is MISSING else emit_yaml(key, value, indent)) + lines[end:]


def _value_at(root: dict):
    def value_at(path):
        node = root
        for part in path:
            if not isinstance(node, dict) or part not in node:
                return MISSING
            node = node[part]
        return node
    return value_at


def _set_paths(lines: list[str], root: dict, paths) -> list[str]:
    """Raw frontmatter lines (--- first and last) with each key path set from root."""
    lines, value_at = list(lines), _value_at(root)
    for path in paths:
        path = tuple(path)
        while len(path) > 1 and value_at(path) is MISSING and value_at(path[:-1]) == {}:
            path = path[:-1]  # its last child gone: write the mapping itself, as {}
        lines = _set_key(lines, 1, len(lines) - 1, 0, path, value_at)
    return lines


def _upsert_frontmatter(lines: list[str], updates: dict, drop_none: bool = True) -> list[str]:
    """Set or drop top-level keys in raw frontmatter lines (a mapping or list is written whole, as a
    block); every other line is kept as written. None drops the key, or with drop_none off writes null."""
    root = {k: v for k, v in updates.items() if v is not None or not drop_none}
    return _set_paths(lines, root, [(key,) for key in updates])


def _inherited_header(lines: list[str], start: int) -> list[str]:
    """The header as written, minus the old file's generated block (or a legacy log's title)."""
    end, counted, in_struck = start, 0, False
    while end < len(lines) and not HEADER_END_RE.match(lines[end]):
        in_struck = bool(STRUCK_RE.match(lines[end])) or (in_struck and lines[end].startswith(">"))
        if not in_struck and ROTATED_RE.search(lines[end]):  # a footer quoted in a struck draft is none
            break
        end += 1
        counted += 0 if in_struck else 1  # struck drafts do not use up the limit
        if counted > HEADER_COPY_LIMIT:
            raise ValueError(f"the header runs past {HEADER_COPY_LIMIT} lines without a Day Plan or beat heading")
    hidden = {i for s, e in struck_spans(lines[start:end]) for i in range(s, e)}  # struck drafts are never copied
    region = [line if line.endswith("\n") else line + "\n" for i, line in enumerate(lines[start:end])
              if not line.startswith(OPENED_MARK) and i not in hidden]  # an opening belongs where it opened
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
    lines = _lines(path.read_text(encoding="utf-8-sig", errors="replace"))
    fm_end = _frontmatter_end([line.rstrip("\n") for line in lines])
    frontmatter = lines[:fm_end + 1] if fm_end is not None else ["---\n", "---\n"]
    frontmatter = [line if line.endswith("\n") else line + "\n" for line in frontmatter]
    frontmatter = _upsert_frontmatter(frontmatter, {"status": None, "shipped": None, "log_dir": log_dir,
                                                    "log_day": day, "continues": rel, "opened": today,
                                                    "currentDate": current_date})
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
              "repaired": None, "set_aside": [], "flag": None, "reason": None, "struck": [], "warnings": []}
    if hops:
        result["warnings"].append(f"{_relative(project, named)} was closed by rotation; using {rel}.")
    blocked = _pending_block(project, rel)
    if blocked:  # before every write: a rotation, a repair or a new session over an unreviewed beat
        result["action"] = "refused"
        result["warnings"].append(blocked)
        if log:
            append_log(project, {"session": rel, "path": "rotate", "action": "refused", "beat": beat,
                                 "reason": "provisional beat pending"})
        return result
    data = read_frontmatter(path)
    file_day, state_day = _days(data, None if data.get("log_day") is not None else _read_lines(path))
    interrupted = _interrupted(project, rel)
    if interrupted and _append_footer(project / interrupted, rel, file_day, today):
        result["repaired"] = interrupted
    if repair_only:  # /rp:end: close what an interrupted rotation left open, write struck drafts, move nothing
        result["file_day"] = file_day
        result["set_aside"] = _set_aside_orphans(project, rel, data, today)
        _write_struck(project, rel, result)
        if log and (result["repaired"] or result["set_aside"] or result["struck"]):
            append_log(project, {"session": rel, "path": "rotate", "action": "none", "repaired": result["repaired"],
                                 "set_aside": result["set_aside"] or None, "struck": result["struck"] or None})
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
        if start:  # drafts a session left without /rp:end go where it ended, before this rotation closes that file
            _write_struck(project, rel, result)
        _rotate_forward(project, path, rel, data, beat_day, today, beat, start, turn_text, result)
    # An earlier crash's unused day file continuing this one (after a rotation the new file is the marker's)
    result["set_aside"] = _set_aside_orphans(project, rel, data, today)
    if start and result["action"] in ("none", "step_back"):
        if str(data.get("status", "")).lower() == "closed":
            result["warnings"].append(f"{rel} was closed by /rp:end; the session reopens it. Set status: open in its "
                                      "frontmatter if play continues there.")
        _swap_marker(project, {"session_file": rel, "session_opened": rel})
    if result["action"] != "refused":  # after the rotation: into the file the beat goes to, before the beat
        _write_struck(project, result["path"], result)
    if start and result["action"] in ("none", "step_back", "rotated"):
        _append_line(project / result["path"], f"{OPENED_MARK} {today} -->")
    if log and (result["action"] != "none" or result["repaired"] or result["set_aside"] or result["struck"]):
        append_log(project, {"session": result["path"], "path": "rotate", "action": result["action"],
                             "day": beat_day, "file_day": file_day, "previous": result["previous"], "beat": beat,
                             "step_back": True if result["action"] == "step_back" else None,
                             "repaired": result["repaired"], "set_aside": result["set_aside"] or None,
                             "struck": result["struck"] or None, "start": True if start else None})
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


# ------------------------------------------------------------ struck drafts
# A draft the gate rejects (one a rewrite replaces, or a surrender's final
# draft) is kept as a record, never as story. strike stashes it whole under
# STRIKE_DIR; rotate, which runs before every shipped beat and at /rp:start and
# /rp:end, writes each stashed draft into the file it names, once, as a STRUCK
# callout. The beat's file is known only when its final draft ships, so a
# strike never writes a log itself, and never a closed one.

STRIKE_DIR = "Work/rp/.gate/struck"
STRUCK_CLOSE = "> <!-- rp-struck: {} -->"  # the block's last line; a block cut short lacks it


def _one_line(value, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def struck_block(draft: str, key: str, beat, attempt: int, reason: str, today: str) -> str:
    """An Obsidian callout, folded: every line starts with ">", so the block ends itself, and no heading,
    footer or marker pattern matches inside it."""
    label = f"Beat {beat}, " if beat else ""
    lines = [f"> [!struck]- STRUCK, not canon: {label}attempt {attempt}, struck {today}. Reason: {reason}", ">"]
    lines += [f"> {line}" if line.strip() else ">" for line in draft.splitlines()]
    lines += [">", STRUCK_CLOSE.format(key)]
    return "\n".join(lines) + "\n"


def strike(project, session_file, *, turn_text, attempt, beat=None, reason=None, today=None, log=True) -> dict:
    """Keep a draft the gate rejected: stash the turn file's DRAFT for rotate to write. Safe to repeat."""
    project = Path(project)
    today = today or datetime.date.today().isoformat()
    named = _inside_sessions(project, str(session_file))
    if named is None or not named.is_file():
        raise ValueError(f"{session_file} is not a session file under {SESSION_DIR}/")
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        raise ValueError("--attempt must be 1 or more")
    draft = parse_turn(turn_text or "")["DRAFT"]
    if not draft.strip():
        raise ValueError("the turn file has no DRAFT to strike")
    beat = _one_line(beat, 40) or None
    reason = _one_line(reason, 300) or "rejected by the gate"
    key = hashlib.sha256(f"{beat or ''}\0{attempt}\0{draft}".encode("utf-8")).hexdigest()[:16]
    rel = _relative(project, named)
    stash = project / STRIKE_DIR
    target = stash / f"{key}.json"
    result = {"action": "already" if _stash_entry(target) else "stashed", "key": key, "session": rel, "beat": beat,
              "attempt": attempt, "stash": _relative(project, target)}
    if result["action"] == "stashed":
        entry = {"key": key, "session": rel, "beat": beat, "attempt": attempt, "reason": reason, "date": today,
                 "created": time.time_ns(), "block": struck_block(draft, key, beat, attempt, reason, today)}
        stash.mkdir(parents=True, exist_ok=True)
        tmp = stash / f".{key}.json.tmp-{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(entry, fh, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)  # whole or absent
    if log:
        append_log(project, {"session": rel, "beat": beat, "attempt": attempt, "path": "strike", "reason": reason,
                             "key": key, "action": result["action"]})
    return result


MAX_CHAIN_BACK = 8  # files walked back from the target for a strike made before a session moved on


def _head_lines(path: Path, limit: int = HEADER_COPY_LIMIT + 2000) -> list[str]:
    """The first lines of a file, enough for its frontmatter and header, without reading a long log whole."""
    try:
        with open(path, encoding="utf-8-sig", errors="replace") as fh:
            return [line.rstrip("\r\n") for line in itertools.islice(fh, limit)]
    except OSError:
        return []


def _walk_back(project: Path, target_rel: str) -> list[str]:
    """target_rel, then the files it continues, newest first: up to MAX_CHAIN_BACK, stopping at a loop."""
    back = [target_rel]
    for _hop in range(MAX_CHAIN_BACK):
        data = read_frontmatter(project / back[-1])
        lines = _head_lines(project / back[-1]) if data.get("log_day") is None else []  # a legacy header
        before = _previous_file(project, data, lines)
        if before is None or before in back:
            break
        back.append(before)
    return back


def _chain_to(project: Path, session_rel: str, target_rel: str, walk: dict | None = None):
    """The files from session_rel to target_rel, oldest first: forward through rotation footers, else back from
    target_rel through its continues (a session that moved on without /rp:end). None when they never meet.
    walk caches the walk back, which is the same for every entry written into one target."""
    path, chain = _inside_sessions(project, str(session_rel)), []
    start = _relative(project, path) if path is not None else None
    while path is not None and path.is_file():
        rel = _relative(project, path)
        if rel in chain:
            break
        chain.append(rel)
        if rel == target_rel:
            return chain
        following = rotation_footer(path)
        path = _inside_sessions(project, following) if following else None
    if start is None or not (project / start).is_file():
        return None
    walk = {} if walk is None else walk
    if "back" not in walk:
        walk["back"] = _walk_back(project, target_rel)
    back = walk["back"]
    return list(reversed(back[:back.index(start) + 1])) if start in back[1:] else None


def _stash_entry(path: Path):
    try:
        entry = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    whole = (isinstance(entry, dict) and all(isinstance(entry.get(k), str) for k in ("key", "session", "block"))
             and isinstance(entry.get("created"), int) and entry["key"] == path.stem
             and re.fullmatch(r"[0-9a-f]{16}", entry["key"]) is not None and STRUCK_RE.match(entry["block"]) is not None
             and entry["block"].endswith(STRUCK_CLOSE.format(entry["key"]) + "\n"))
    return entry if whole else None


def _append_struck(path: Path, block: str, closing: str):
    """One struck block in one write, under the lock: True written, False already there, None (nothing written)
    when the file is closed."""
    with open(path, "ab") as fh:
        if fcntl:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        if rotation_footer(path):
            return None
        if closing in (_read_lines(path) or []):
            return False
        size = fh.seek(0, os.SEEK_END)
        lead = ""
        if size:
            with open(path, "rb") as tail:
                tail.seek(max(0, size - 2))
                end = tail.read()
            lead = "" if end.endswith(b"\n\n") or end == b"\n" else "\n" if end.endswith(b"\n") else "\n\n"
        fh.write((lead + block + "\n").encode("utf-8"))  # a blank line ends the callout
        fh.flush()
        os.fsync(fh.fileno())
    return True


def _write_struck(project: Path, target_rel: str, result: dict) -> None:
    """Write the stashed drafts of target's run into it, oldest first, each once, then clear them. Entries of
    another run stay; one whose file is gone, or that cannot be read, stays and is reported. Never raises: a
    struck draft is a record, and it never keeps a beat from shipping."""
    result.setdefault("struck", [])
    try:
        _write_stash(project, target_rel, result)
    except (OSError, ValueError) as exc:
        result["warnings"].append(f"Struck drafts were not all written ({exc}); those not written stay in the "
                                  "stash for the next rotate.")


def _write_stash(project: Path, target_rel: str, result: dict) -> None:
    stash = project / STRIKE_DIR
    entries = []
    for path in sorted(stash.glob("*.json")) if stash.is_dir() else ():
        entry = _stash_entry(path)
        if entry is None:
            result["warnings"].append(f"{_relative(project, path)} is not a whole struck draft; left in place.")
            continue
        entries.append((entry["created"], entry.get("attempt") or 0, path.name, path, entry))
    target, walk = project / target_rel, {}
    for _created, _attempt, _name, path, entry in sorted(entries, key=lambda e: e[:3]):
        try:
            chain = _chain_to(project, entry["session"], target_rel, walk)
            named = None if chain else _inside_sessions(project, entry["session"])
        except (OSError, ValueError) as exc:  # a path no file can have
            result["warnings"].append(f"{_relative(project, path)} names no session file ({exc}); left in place.")
            continue
        if chain is None:
            if named is None or not named.is_file():
                result["warnings"].append(f"A struck draft ({path.name}) was struck in {entry['session']}, which no "
                                          "longer exists; it stays in the stash.")
            continue  # another run's: it waits for its own rotate
        closing = STRUCK_CLOSE.format(entry["key"])
        try:
            if not any(closing in (_read_lines(project / rel) or []) for rel in chain[:-1]):
                written = _append_struck(target, entry["block"], closing)
                if written is None:
                    result["warnings"].append(f"{target_rel} is closed by rotation; no struck draft is written after "
                                              "its footer. They stay in the stash.")
                    return
        except (OSError, ValueError) as exc:
            result["warnings"].append(f"{_relative(project, path)} could not be written ({exc}); it stays in the stash.")
            continue
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            result["warnings"].append(f"{_relative(project, path)} was written but not cleared ({exc}); the next "
                                      "rotate clears it without writing it twice.")
        result["struck"].append(entry["key"])


# ------------------------------------------------- ship, state block, summaries
# After rotate, ship appends the beat and applies its gated delta in one
# replace, so state never moves without its beat. After a rotation, state
# --refresh copies the frontmatter's run: block into the run state file, and
# the closed day gets its summary, published once. Each step is safe to repeat.

REWRITE_TRIES = 3
STATE_START = "<!-- rp-run-state:"
STATE_END = "<!-- /rp-run-state -->"
STATE_START_RE = re.compile(r"^<!-- rp-run-state: .*\bsha256:([0-9a-f]{12}) -->\s*$")
SUMMARY_DIR = "Work/rp/summaries"
DAY_HEADING_RE = re.compile(r"^##\s+Day\s+(\d+)\b")
LINE_CITE_RE = re.compile(r"(?<![\w./-])([\w./-]+\.md):(\d+)(?:[-–](\d+))?")  # a path with spaces is not checked
BEAT_CITE_RE = re.compile(r"(?<!\w)(?:B|Beat\s+)(\d+)(?!\d)")


class StateBlockEdited(ValueError):
    pass


def _stat_key(path: Path):
    st = os.stat(path)
    return st.st_ino, st.st_size, st.st_mtime_ns


def _lines(text: str) -> list[str]:
    """Lines with their endings, split at \\n only: a U+2028 inside a value is not a line break here."""
    parts = text.split("\n")
    return [part + "\n" for part in parts[:-1]] + ([parts[-1]] if parts[-1] else [])


def _newline(lines: list[str]) -> str:
    return "\r\n" if lines and lines[0].endswith("\r\n") else "\n"


def _as_newline(lines: list[str], nl: str) -> list[str]:
    """Lines ending in the file's own line break (a CRLF file stays CRLF)."""
    return [line[:-1].rstrip("\r") + nl if line.endswith("\n") else line for line in lines] if nl != "\n" else lines


def _replace_text(path: Path, make) -> bool:
    """Rewrite a file whole (temp file, then os.replace): a reader sees the old file or the new.

    make(old) returns the new text, or None for no change. If the file changes
    while the new text is being made (a hand edit in another tool), start again
    from the fresh file rather than write over the edit. Every byte make leaves
    alone is kept: a BOM, CRLF endings, bytes that are not UTF-8.
    """
    for _attempt in range(REWRITE_TRIES):
        before = _stat_key(path)
        raw = path.read_bytes()
        bom = raw.startswith(b"\xef\xbb\xbf")
        old = raw[3 if bom else 0:].decode("utf-8", "surrogateescape")
        new = make(old)
        if new is None or new == old:
            return False
        tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
        try:
            with open(tmp, "wb") as fh:
                fh.write((b"\xef\xbb\xbf" if bom else b"") + new.encode("utf-8", "surrogateescape"))
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, stat.S_IMODE(os.stat(path).st_mode))
            if _stat_key(path) != before:
                continue
            os.replace(tmp, path)
            return True
        finally:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
    raise OSError(f"{path} kept changing while it was being rewritten; nothing was written. Run the command again")


def _delta_updates(data: dict, delta: dict, config: dict, beat_day=None):
    """(root, paths, applied, dropped): the frontmatter after a gated delta, and the key paths it changed
    (root mirrors synced from scene.*).

    An entry the lint would still flag as a key (an unknown key, a root mirror,
    a malformed participants list, an invalid run entry) is dropped and named.
    beat_day bounds clocks as the lint does: the delta's day, the anchor's, then
    the frontmatter's.
    """
    known = DELTA_KEYS | set(config["deltaKeys"])
    beat_day = beat_day if beat_day is not None else fiction_day(data.get("currentDate"))
    run_findings, _events = run_delta_findings(delta, data.get("run") if "run" in data else None, beat_day)
    bad = {f["key"]: f["check"] for f in run_findings}
    root = copy.deepcopy(data)
    paths, applied, dropped = [], [], []
    for key, value in delta.items():
        check = None
        if key == "run" or key.startswith("run."):
            check = bad.get(key)
        elif key not in known:
            check = "delta_root_mirror" if key in ROOT_MIRRORS else "delta_unknown_key"
        elif key == "scene.participants" and not isinstance(value, list):
            check = "delta_malformed"
        if check:
            dropped.append({"key": key, "check": check})
            continue
        applied.append(key)
        path = tuple(key.split(".", 2)) if key.startswith("run.") else tuple(key.split(".", 1))
        node = root
        for part in path[:-1]:
            if not isinstance(node.get(part), dict):
                node[part] = {}
            node = node[part]
        if value is None and path[0] == "run":
            node.pop(path[-1], None)  # a body part gone, a clock fired
        else:
            node[path[-1]] = value
        paths.append(path)
    scene = root.get("scene") if isinstance(root.get("scene"), dict) else {}
    for key, mirror, value in (("scene.time", "currentTime", lambda: scene.get("time")),
                               ("scene.location", "currentLocation", lambda: scene.get("location")),
                               ("scene.participants", "participants",
                                lambda: _participant_names(scene.get("participants")))):
        if key in applied:
            root[mirror] = value()
            paths.append((mirror,))
    return root, paths, applied, dropped


def _ship_text(old: str, delta: dict, append_text, mark: str, config: dict, beat_day=None):
    """(new text, applied, dropped) for one shipped beat; (None, [], []) when this beat already shipped.
    Only the frontmatter lines the delta changes are rewritten; the body is kept byte for byte."""
    lines = _lines(old)
    plain = [line.rstrip("\r\n") for line in lines]
    fm_end = _frontmatter_end(plain)
    if fm_end is None:
        raise ValueError("the session file has no YAML frontmatter to apply the delta to; nothing was shipped")
    data = parse_yaml("".join(lines[1:fm_end]))
    data = data if isinstance(data, dict) else {}
    if str(data.get("shipped") or "") in (mark, mark + SOFT_MARK):  # shipped, or shipped and corrected by settle
        return None, [], []
    if _final_footer(plain):  # re-checked on every try: a rotation may have closed it meanwhile
        raise ValueError("the session file was closed by rotation; it is never written again. Ship to the file "
                         "rotate named")
    root, paths, applied, dropped = _delta_updates(data, delta, config, beat_day)
    root["shipped"] = mark
    nl = _newline(lines)
    head = [line if line.endswith("\n") else line + nl for line in lines[:fm_end + 1]]
    head = _as_newline(_set_paths(head, root, paths + [("shipped",)]), nl)
    written = parse_yaml("".join(head[1:-1]))
    written = written if isinstance(written, dict) else {}  # a frontmatter that reads as a list: refused below
    unread = []
    for k in sorted(k for k in set(written) | set(root) if written.get(k, MISSING) != root.get(k, MISSING)):
        leaves = [".".join(x) for x in paths + [("shipped",)]
                  if x[0] == k and _value_at(written)(x) != _value_at(root)(x)]
        unread += leaves or [k]
    if unread:  # every key, changed or not: a rewrite must leave the rest of the frontmatter reading as it did
        raise ValueError(f"the frontmatter would not read back as meant after this ship ({', '.join(unread)}): a "
                         "value the gate's YAML cannot hold, or a frontmatter line it cannot place (a stray carriage "
                         "return, uneven indentation). Nothing was shipped; fix the delta or the frontmatter line")
    body = "".join(lines[fm_end + 1:])
    if append_text:
        text = append_text.replace("\r\n", "\n")
        text = (text if text.endswith("\n") else text + "\n").replace("\n", nl)
        if body and not body.endswith("\n"):
            body += nl
        if body.strip() and not body.endswith(nl + nl):
            body += nl
        body += text
    return "".join(head) + body, applied, dropped


def _ship_inputs(turn_text: str, append_text, beat):
    """(turn, delta, beat_day, label, digest, mark) for one beat: what ship applies, and its shipped mark.
    The digest covers the label, the appended text and the delta, so a re-run is the same beat."""
    turn = parse_turn(turn_text)
    delta, parsed = parse_delta(turn.get("SCENE_STATE_DELTA", ""))
    if not parsed:
        raise ValueError("SCENE_STATE_DELTA is not a key: value mapping; nothing was shipped")
    fields = _anchor(turn.get("DRAFT", ""), []) if turn.get("DRAFT", "").strip() else None
    beat_day = _turn_day(delta, (fields or {}).get("scene"), None)
    label = str(beat).strip() if beat else ""
    key = "\0".join([label, append_text or "", json.dumps(delta, sort_keys=True, ensure_ascii=False, default=str)])
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]
    return turn, delta, beat_day, label, digest, f"{label}@{digest}"


def ship(project, session_file, *, turn_text: str, append_text=None, beat=None, today=None, config=None,
         log=True, provisional=False, attempt=1, max_rewrites=None, reviewed=False) -> dict:
    """Ship a gated beat: append its text (the new day's Day Plan first, if any) and apply its delta to the
    open file's frontmatter, in one replace. Never a closed file; a retry after success changes nothing.

    provisional ships a first draft before its review (see settle): the text goes in between rp-provisional
    marks, and the pending review keeps a byte copy of the file as it was, which the background reviewer
    reads. Nothing ships over a provisional beat until it is settled. reviewed (a beat the reviewer passed)
    consumes the findings a soft background review carried into its draft."""
    project = Path(project)
    config = config or load_config(project)
    path = _inside_sessions(project, str(session_file))
    if path is None or not path.is_file():
        raise ValueError(f"{session_file} is not a session file under {SESSION_DIR}/")
    rel = _relative(project, path)
    if rotation_footer(path):
        raise ValueError(f"{rel} is closed: rotation footered it, and it is never written again. Ship to the "
                         "file rotate named")
    turn, delta, beat_day, label, digest, mark = _ship_inputs(turn_text, append_text, beat)
    blocked = _pending_block(project, rel, exclude=digest)
    if blocked:
        raise ValueError(blocked)
    result = {"action": "already_shipped", "path": rel, "beat": label or None, "shipped": mark, "applied": [],
              "dropped": []}
    text, snapshot = append_text, None
    if provisional:
        _provisional_preflight(project, path, turn_text, append_text, attempt, max_rewrites)
        text = f"{_provisional_open(digest)}\n{_with_newline(append_text)}{_provisional_close(digest)}\n"
        snapshot = _write_pending(project, path, rel, turn, turn_text, append_text, digest, mark, label, attempt,
                                  max_rewrites, today, config)
        result["key"] = digest
        if snapshot[0] is not None:
            result.update(review_session=_relative(project, snapshot[0]), pending=_relative(project,
                                                                                            snapshot[0].parent))
            kept = snapshot[0].parent / "brief.md"
            result["review_brief"] = _relative(project, kept) if kept.is_file() else None
            result["review_brief_reads"] = _kept_reads(kept.parent)

    def make(old):
        if snapshot is not None and snapshot[1] is not None and old != snapshot[1]:  # moved after its copy
            raise OSError(f"{rel} changed while the beat was being shipped; nothing was shipped. Run it again")
        new, applied, dropped = _ship_text(old, delta, text, mark, config, beat_day)
        result.update(action="shipped" if new is not None else "already_shipped", applied=applied, dropped=dropped)
        return new

    _replace_text(path, make)
    if result["action"] == "shipped" and reviewed and not provisional:
        _clear_carried(project, rel)  # the findings carried into this beat's draft were reviewed with it
    if log and result["action"] == "shipped":
        append_log(project, {"session": rel, "path": "ship", "beat": label or None, "applied": len(result["applied"]),
                             "dropped": [d["key"] for d in result["dropped"]] or None,
                             "provisional": True if provisional else None, "key": digest if provisional else None})
    return result


# ------------------------------------------------------- provisional beats
# On a harness that can run the review in the background, a first draft whose
# lint is clean ships at once (ship --provisional) and is shown; its review runs
# on a frozen copy of the file it was drafted against, and the next /rp:turn
# settles the verdict before anything else is drafted. The provisional text sits
# between two marks in its file. settle clean or soft drops the marks (soft also
# carries the findings to the next draft); a confirmed hard finding replaces the
# marked text with a STRUCK callout and restores the frontmatter the beat
# changed, in one write, after checking neither was edited since. Nothing ships,
# rotates, refreshes the state block or is summarised over a provisional beat.

PENDING_DIR = "Work/rp/.gate/pending"
CARRIED_DIR = "Work/rp/.gate/carried"  # one file per run: carried_path
QUEUED_INPUT = "Work/rp/.gate/queued-input.md"  # the Keeper's input held when the beat it answered was struck
PROVISIONAL_RE = re.compile(r"^<!-- rp-provisional: ([0-9a-f]{12}) -->\s*$")
VERDICTS = ("clean", "soft", "struck")
SOFT_MARK = "+soft"  # the shipped mark after a soft settle corrected the frontmatter
MAX_WAIT = 300  # seconds pending --wait sleeps at most


def _provisional_open(key: str) -> str:
    return f"<!-- rp-provisional: {key} -->"


def _provisional_close(key: str) -> str:
    return f"<!-- /rp-provisional: {key} -->"


def _with_newline(text: str) -> str:
    text = text.replace("\r\n", "\n")
    return text if text.endswith("\n") else text + "\n"


def provisional_keys(lines) -> list[str]:
    """The provisional beats a file holds, by key, in order."""
    return [m.group(1) for line in lines or [] if (m := PROVISIONAL_RE.match(line))]


def _pending_block(project: Path, rel: str, exclude=None):
    """Why nothing may ship, rotate or be summarised over rel now, or None: a provisional beat in it, or a pending
    review whose beat it holds although the marks are gone (removed by hand)."""
    lines = _read_lines(project / rel)
    keys = [k for k in provisional_keys(lines) if k != exclude]
    if not keys and lines:
        shipped = _shipped_of("\n".join(lines[:2001]))
        keys = [e["key"] for e in _pending_metas(project, rel) if e["key"] != exclude
                and shipped in (e["mark"], e["mark"] + SOFT_MARK) and not _settled(project, e["key"])]
    if not keys:
        return None
    return (f"{rel} holds a provisional beat whose review is pending (key {keys[0]}): settle it first "
            "(rp_gate.py pending, then settle with its verdict); nothing ships, rotates or is summarised over it")


def _pending_metas(project: Path, rel=None) -> list[dict]:
    root = project / PENDING_DIR
    metas = [_pending_meta(folder / "meta.json") for folder in sorted(root.iterdir())] if root.is_dir() else []
    return [m for m in metas if m is not None and (rel is None or m["session"] == rel)]


def _settled(project: Path, key: str):
    """The verdict a settle wrote into the file before it could clear the pending review (a crash between)."""
    try:
        data = json.loads((project / PENDING_DIR / key / "settled.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("verdict") in VERDICTS else None


def _day_change(project: Path, path: Path, turn_text: str):
    """(beat day, file day) when the turn's beat would land on another fiction day than its file's, else None."""
    data = read_frontmatter(path)
    file_day, state_day = _days(data, None if data.get("log_day") is not None else _read_lines(path))
    beat_day, _source = _beat_day(None, turn_text, state_day, None)
    if beat_day is not None and file_day is not None and beat_day != file_day:
        return beat_day, file_day
    return None


def _provisional_preflight(project: Path, path: Path, turn_text: str, append_text, attempt, max_rewrites) -> None:
    if not append_text:
        raise ValueError("a provisional ship needs the beat's text (--append-file)")
    if not isinstance(max_rewrites, int) or isinstance(max_rewrites, bool) or max_rewrites < 1:
        raise ValueError("a provisional ship needs --max-rewrites (route.max_rewrites, 1 or more): a struck beat is "
                         "redrafted within it, and with none left the review runs before the beat is shown")
    if attempt != 1:
        raise ValueError("only a first draft ships provisionally: a rewrite answers a confirmed violation and is "
                         "reviewed before it is shown")
    moved = _day_change(project, path, turn_text)
    if moved:
        raise ValueError(f"the beat moves the fiction day (Day {moved[1]} to Day {moved[0]}): a rotation cannot be "
                         "undone, so it is reviewed before it ships; nothing was shipped")


def _write_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _decoded(raw: bytes) -> str:
    """A file's text as _replace_text hands it to make: without a BOM, bytes that are not UTF-8 kept."""
    return raw[3 if raw.startswith(b"\xef\xbb\xbf") else 0:].decode("utf-8", "surrogateescape")


def _write_pending(project, path, rel, turn, turn_text, append_text, digest, mark, label, attempt, max_rewrites,
                   today, config=None):
    """The pending review, written before the beat: a byte copy of the file as it is (what the beat was drafted
    against), the turn, the beat's text, then meta.json, which makes it whole. (snapshot path, its text)."""
    folder = project / PENDING_DIR / digest
    snapshot = folder / path.name
    raw = path.read_bytes()
    meta_path, current = folder / "meta.json", _decoded(raw)
    if _pending_meta(meta_path) is None and _shipped_of(current) in (mark, mark + SOFT_MARK):
        return None, None  # shipped and settled already (a retry): there is nothing left to review
    if _pending_meta(meta_path) is not None and snapshot.is_file():
        if _shipped_of(current) == mark:  # shipped already: keep the copy from before it
            return snapshot, None
        if _shipped_of(_decoded(snapshot.read_bytes())) != _shipped_of(current):
            raise ValueError(f"{rel} moved on since this beat's pending review was written (another beat shipped); "
                             f"nothing was shipped. Settle {digest} with its verdict (a beat never shown is dropped), "
                             "then draft this beat again")
    folder.mkdir(parents=True, exist_ok=True)
    _write_atomic(snapshot, raw)
    _write_atomic(folder / "turn.md", turn_text.encode("utf-8"))
    _write_atomic(folder / "beat.md", append_text.encode("utf-8"))
    try:
        check = json.loads((project / CHECK_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        check = None
    if isinstance(check, dict) and check.get("turn") == _turn_digest(turn_text):  # the lint this beat was routed on
        _write_atomic(folder / "check.json", json.dumps(check, ensure_ascii=False).encode("utf-8"))
    meta = {"key": digest, "mark": mark, "session": rel, "beat": label or None, "attempt": attempt,
            "max_rewrites": max_rewrites, "created": time.time_ns(),
            "date": today or datetime.date.today().isoformat(),
            "keeper_input": (turn.get("KEEPER_INPUT") or "").strip(), "snapshot": snapshot.name}
    _copy_brief(project, folder, snapshot, path, turn, config, label)
    _write_atomic(meta_path, json.dumps(meta, ensure_ascii=False).encode("utf-8"))
    return snapshot, _decoded(raw)


def _copy_brief(project: Path, folder: Path, snapshot: Path, live: Path, turn: dict, config, label) -> None:
    """The brief for the review: built from the copy (the bytes this beat was drafted against) and this turn's
    input, so every path, heading and beat tag in it names the copy; never a brief file that may be stale. Its
    report goes beside it, so the reviewer reads it in the parts the GM did (its reads)."""
    try:
        text, report = build_brief(project, _relative(project, snapshot), turn.get("KEEPER_INPUT", "") or "",
                                   config=config or load_config(project), beat=label or None, source_dir=live.parent)
    except (OSError, ValueError):
        return  # the review then reads the copy's LIVE_STATE without a brief
    (folder / "brief.md").unlink(missing_ok=True)  # never a brief beside another build's report
    _write_atomic(folder / "brief.json", json.dumps(report, ensure_ascii=False).encode("utf-8"))
    _write_atomic(folder / "brief.md", text.encode("utf-8"))


def _kept_reads(folder: Path):
    """The reads of a pending review's brief, from its report; None without a brief or a report that names them."""
    if not (folder / "brief.md").is_file():
        return None
    try:
        reads = json.loads((folder / "brief.json").read_text(encoding="utf-8")).get("reads")
    except (OSError, ValueError, AttributeError):
        return None
    return reads if isinstance(reads, list) else None


def _shipped_of(text: str) -> str:
    """The shipped mark in a session text's frontmatter: the last beat ship wrote into it."""
    plain = text.splitlines()
    end = _frontmatter_end(plain)
    data = parse_yaml("\n".join(plain[1:end])) if end else None
    return str(data.get("shipped") or "") if isinstance(data, dict) else ""


def _pending_meta(path: Path):
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    whole = (isinstance(meta, dict) and isinstance(meta.get("key"), str) and re.fullmatch(r"[0-9a-f]{12}", meta["key"])
             and meta["key"] == path.parent.name and all(isinstance(meta.get(k), str)
                                                         for k in ("mark", "session", "snapshot")))
    return meta if whole else None


def pending_reviews(project, session=None, detail=False, beats=LOG_DEFAULTS["previousBeats"]) -> list[dict]:
    """The pending reviews, oldest first; with session, only that file's. shipped: its text is in the file.
    detail adds live_state: the LIVE_STATE block of the copy taken before the ship, what its review reads."""
    project = Path(project)
    root, found = project / PENDING_DIR, []
    for folder in sorted(root.iterdir()) if root.is_dir() else ():
        meta = _pending_meta(folder / "meta.json")
        if meta is None or (session is not None and meta["session"] != session):
            continue
        lines = _read_lines(project / meta["session"]) or []
        marked = meta["key"] in provisional_keys(lines)
        in_file = marked or _shipped_of("\n".join(lines[:2001])) in (meta["mark"], meta["mark"] + SOFT_MARK)
        found.append({"key": meta["key"], "session": meta["session"], "beat": meta.get("beat"),
                      "attempt": meta.get("attempt"), "max_rewrites": meta.get("max_rewrites"),
                      "created": meta.get("created"), "date": meta.get("date"),
                      "age_s": round(max(0, time.time_ns() - int(meta.get("created") or 0)) / 1e9, 1),
                      "shipped": in_file, "marks": marked, "settled": _settled(project, meta["key"]) is not None,
                      "keeper_input": meta.get("keeper_input") or "",
                      "review_session": _relative(project, folder / meta["snapshot"]),
                      "turn": _relative(project, folder / "turn.md"),
                      "beat_text": _relative(project, folder / "beat.md"),
                      "check": _relative(project, folder / "check.json") if (folder / "check.json").is_file() else None,
                      "brief": _relative(project, folder / "brief.md") if (folder / "brief.md").is_file() else None,
                      "brief_reads": _kept_reads(folder)})
        if detail:
            snapshot = found[-1]["review_session"]
            found[-1]["live_state"] = live_state_block(live_state(project, snapshot),
                                                       continuity(project, snapshot, beats), snapshot)
    return sorted(found, key=lambda e: (e["created"] or 0, e["key"]))


def carried_path(project, session_file) -> Path:
    """The run's carried findings: one file per run (its log_dir, which every day file of it names, else its file),
    so another campaign's brief never reads them and its ship never clears them."""
    project = Path(project)
    rel = _relative(project, project / session_file)
    log_dir, _warning = _log_dir(project, read_frontmatter(project / rel))
    run = log_dir or rel
    return project / CARRIED_DIR / f"{hashlib.sha256(run.encode('utf-8')).hexdigest()[:12]}.md"


def _clear_carried(project: Path, rel: str) -> None:
    carried_path(project, rel).unlink(missing_ok=True)


def _write_carried(project: Path, rel: str, meta: dict, findings: str, today: str) -> None:
    beat = f"Beat {meta['beat']}" if meta.get("beat") else "the last beat"
    text = (f"<!-- rp-carried: {meta['key']} {rel} -->\n# Carried findings: {beat} (soft, reviewed after it was "
            f"shown, {today})\n\nThe beat stands as shown; any state they named was corrected in the frontmatter. "
            f"Correct the rest in the next beat, going forward: they are not edits to the shown beat.\n\n"
            f"{findings.strip()}\n")
    path = carried_path(project, rel)
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_atomic(path, text.encode("utf-8"))


class _Refused(ValueError):
    pass


def _region(lines: list[str], key: str):
    """(open index, close index) of a provisional beat's marks in lines (endings stripped), or None."""
    opening, closing = _provisional_open(key), _provisional_close(key)
    start = next((i for i, line in enumerate(lines) if line.rstrip("\r\n") == opening), None)
    if start is None:
        return None
    end = next((i for i in range(start + 1, len(lines)) if lines[i].rstrip("\r\n") == closing), None)
    if end is None:
        raise ValueError(f"the provisional beat {key} has lost its closing mark ({closing}); put it back after the "
                         "beat by hand, then settle again")
    return start, end


def _expected_after_ship(folder: Path, meta: dict, config: dict):
    """(turn_text, beat_text, snapshot_text, frontmatter lines the provisional ship wrote, delta, beat_day): what
    the file must still hold for a settle to rewrite its frontmatter."""
    turn_text = (folder / "turn.md").read_text(encoding="utf-8")
    beat_text = (folder / "beat.md").read_text(encoding="utf-8")
    snapshot_text = _decoded((folder / meta["snapshot"]).read_bytes())
    _turn, delta, beat_day, _label, _digest, mark = _ship_inputs(turn_text, beat_text, meta.get("beat"))
    wrapped = f"{_provisional_open(meta['key'])}\n{_with_newline(beat_text)}{_provisional_close(meta['key'])}\n"
    expected, _applied, _dropped = _ship_text(snapshot_text, delta, wrapped, mark, config, beat_day)
    if expected is None or mark != meta["mark"]:
        raise ValueError(f"the pending review {meta['key']} does not match its own ship; it stays. Ask the Keeper")
    want = [x.rstrip("\r\n") for x in _lines(expected)]
    return turn_text, beat_text, snapshot_text, want[:_frontmatter_end(want) + 1], beat_day


def _check_shipped(have: list[str], want_head: list[str], key: str, beat_text: str):
    """(start, end) of the provisional beat in have, after checking the frontmatter and the beat's text are
    exactly what its ship wrote; _Refused otherwise."""
    plain = [x.rstrip("\r\n") for x in have]
    fm_end = _frontmatter_end(plain)
    if fm_end is None or plain[:fm_end + 1] != want_head:
        raise _Refused("the frontmatter changed since the provisional ship (a hand edit?)")
    found = _region(have, key)
    if found is None:
        raise _Refused("its marks are gone")
    start, end = found
    if plain[start + 1:end] != _with_newline(beat_text).split("\n")[:-1]:  # split as _lines does: at \n only
        raise _Refused("the provisional beat's text changed since it shipped (a hand edit?)")
    return start, end


def settle(project, key, *, verdict, violations=None, reason=None, findings_text=None, delta_text=None, today=None,
           config=None, log=True) -> dict:
    """Apply a background review's verdict to the provisional beat it reviewed, once.

    clean drops the beat's marks: it stands as shown. soft does the same and carries the findings to the next
    draft; delta_text, a correction for the state they name, is applied to the frontmatter in the same write.
    struck replaces the marked text with a STRUCK callout of the beat as it shipped and restores the frontmatter
    from the copy taken before it, in one replace. A frontmatter rewrite happens only while the frontmatter and the
    beat are exactly as the ship wrote them (refused otherwise, nothing written). A key that is not pending is
    stale: nothing is written."""
    project = Path(project)
    today = today or datetime.date.today().isoformat()
    config = config or load_config(project)
    if verdict not in VERDICTS:
        raise ValueError(f"--verdict must be one of {', '.join(VERDICTS)}")
    if not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{12}", key):
        raise ValueError("--key must be the 12-hex key ship --provisional printed")
    violations = [v for v in (violations or []) if v]
    correction, parsed = parse_delta(delta_text or "")
    if delta_text and (verdict != "soft" or not parsed or not correction):
        raise ValueError("--delta-file goes with --verdict soft and holds a SCENE_STATE_DELTA mapping")
    folder = project / PENDING_DIR / key
    meta = _pending_meta(folder / "meta.json")
    result = {"action": "stale", "key": key, "verdict": verdict, "session": None, "beat": None, "attempt": None,
              "max_rewrites": None, "keeper_input": None, "warnings": []}
    if meta is None:
        result["warnings"].append(f"No review is pending for {key} (settled already, or never shipped): this "
                                  "verdict is not applied.")
        if log:
            append_log(project, {"path": "settle", "key": key, "verdict": verdict, "action": "stale"})
        return result
    path = _inside_sessions(project, meta["session"])
    if path is None or not path.is_file():
        raise ValueError(f"{meta['session']} is not a session file under {SESSION_DIR}/; the pending review stays")
    rel = _relative(project, path)
    result.update(session=rel, beat=meta.get("beat"), attempt=meta.get("attempt"),
                  max_rewrites=meta.get("max_rewrites"), keeper_input=meta.get("keeper_input") or "")
    attempt = meta.get("attempt") if isinstance(meta.get("attempt"), int) else 1
    beat_text = (folder / "beat.md").read_text(encoding="utf-8")
    struck_key = hashlib.sha256(f"{meta.get('beat') or ''}\0{attempt}\0{beat_text}".encode("utf-8")).hexdigest()[:16]
    lines = _read_lines(path) or []

    def finish(applied: str, carried: dict) -> dict:
        """After the file holds the verdict: record it, carry the findings, clear the pending review."""
        _write_atomic(folder / "settled.json", json.dumps({"verdict": applied, **carried}).encode("utf-8"))
        if applied == "soft" and carried.get("findings"):
            _write_carried(project, rel, meta, carried["findings"], today)
        elif applied in ("clean", "soft"):
            _clear_carried(project, rel)
        shutil.rmtree(folder)
        if log:
            append_log(project, {"session": rel, "path": "settle", "beat": meta.get("beat"), "key": key,
                                 "verdict": verdict, "action": result["action"], "violations": violations or None})
        return result

    carried = {"findings": findings_text or "\n".join(f"- {v}" for v in violations)} if verdict == "soft" else {}
    if _region(lines, key) is None:
        done = _settled(project, key)
        if done is None and STRUCK_CLOSE.format(struck_key) in lines:
            done = {"verdict": "struck"}
        if done is not None:  # a settle wrote the file, then stopped before it cleared the review
            result["action"] = "already"
            if (done["verdict"] == "struck") != (verdict == "struck"):
                result["warnings"].append(f"Beat {meta.get('beat') or key} was already settled {done['verdict']}; "
                                          f"this {verdict} verdict is not applied.")
            return finish(done["verdict"], {k: v for k, v in done.items() if k != "verdict"})
        snapshot_shipped = _shipped_of(_decoded((folder / meta["snapshot"]).read_bytes()))
        if _shipped_of("\n".join(lines[:2001])) != snapshot_shipped:
            result["action"] = "refused"
            result["warnings"].append(f"Beat {meta.get('beat') or key}'s rp-provisional marks are gone from {rel}, "
                                      "but the file holds the beat or has moved on (marks removed by hand?). Nothing "
                                      "was written and its review stays pending: put the marks back around the beat, "
                                      "or ask the Keeper.")
            return result
        if verdict != "struck":  # never shipped, so never shown: ship it, then settle
            result["action"] = "unshipped"
            result["warnings"].append(f"Beat {meta.get('beat') or key} never reached {rel}: run ship --provisional "
                                      f"again with the files in {_relative(project, folder)} and the same --beat and "
                                      "--max-rewrites, then settle.")
            return result
        turn_text = (folder / "turn.md").read_text(encoding="utf-8")
        strike(project, rel, turn_text=turn_text, attempt=attempt, beat=meta.get("beat"),
               reason=_one_line(reason or ", ".join(violations) or "struck", 300), today=today, log=log)
        result["action"] = "dropped"  # struck before it was shown: kept as any rejected draft is kept
        shutil.rmtree(folder)
        if log:
            append_log(project, {"session": rel, "path": "settle", "beat": meta.get("beat"), "key": key,
                                 "verdict": verdict, "action": "dropped", "violations": violations or None})
        return result
    if rotation_footer(path):
        raise ValueError(f"{rel} is closed by rotation; it is never written again, and its provisional beat stays "
                         "pending. Ask the Keeper")
    if verdict == "struck" or correction:
        _turn_text, _beat, snapshot_text, want_head, beat_day = _expected_after_ship(folder, meta, config)
    if verdict == "struck":
        block = struck_block(beat_text, struck_key, meta.get("beat"), attempt,
                             _one_line("shown, then struck: " + (reason or ", ".join(violations) or "hard"), 300),
                             today)

        def make(old):
            have = _lines(old)
            nl = _newline(have)
            start, end = _check_shipped(have, want_head, key, beat_text)
            before_lines = _lines(snapshot_text)
            snap_end = _frontmatter_end([x.rstrip("\r\n") for x in before_lines])
            fm_end = _frontmatter_end([x.rstrip("\r\n") for x in have])
            tail = have[end + 1:]
            stand_in = _as_newline(_lines(block + ("\n" if tail else "")), nl)
            return "".join(_as_newline(before_lines[:snap_end + 1], nl) + have[fm_end + 1:start] + stand_in + tail)
    else:
        def make(old):
            have = _lines(old)
            if correction:
                start, end = _check_shipped(have, want_head, key, beat_text)
            else:
                found = _region(have, key)
                if found is None:
                    return None
                start, end = found
            text = "".join(have[:start] + have[start + 1:end] + have[end + 1:])
            if correction:  # the state the findings named, corrected in the same write; the shown prose stays
                new, applied, dropped = _ship_text(text, correction, None, meta["mark"] + SOFT_MARK, config, beat_day)
                result.update(corrected=applied, dropped=dropped)
                text = new if new is not None else text
            return text

    try:
        _replace_text(path, make)
    except _Refused as exc:
        result["action"] = "refused"
        what = "struck" if verdict == "struck" else "corrected"
        result["warnings"].append(f"Beat {meta.get('beat') or key} was not {what}: {exc}. Nothing was written and its "
                                  "review stays pending; show the Keeper the verdict.")
        return result
    if verdict == "struck":
        result["action"] = "struck"
        if log:
            append_log(project, {"session": rel, "beat": meta.get("beat"), "attempt": attempt, "path": "strike",
                                 "reason": _one_line(reason or ", ".join(violations) or "hard", 300), "key": struck_key,
                                 "action": "struck", "shipped": True, "provisional_key": key})
    else:
        result["action"] = "settled"
    return finish("soft" if verdict == "soft" else verdict, carried)


def background_route(decision: dict, triggers: list[str], result: dict, config: dict, project: Path, session: str,
                     turn_text: str, rewrite: bool, harness=None) -> dict:
    """Whether this beat may ship before its review, which then runs in the background: only a first draft the
    lint passed, sent to review because it is consequential, on its own fiction day, on a verified harness."""
    if decision["next"] != "review":
        return {"eligible": False, "reasons": ["not_reviewed"]}
    reasons = []
    if not config.get("background", True):
        reasons.append("policy")
    harness = os.environ.get("ASHA_HARNESS", "") if harness is None else harness
    if not harness:
        reasons.append("harness_unknown")
    elif harness not in BACKGROUND_HARNESSES:
        reasons.append("harness")
    if rewrite:
        reasons.append("rewrite")
    if decision.get("max_rewrites", 0) < 1:  # a confirmed hard finding would surrender, before the Keeper's eyes
        reasons.append("no_rewrites")
    if "validate_tag" in triggers:
        reasons.append("validate_tag")
    if any(f["fix"] == "review" or (f["fix"] == "edit" and not f.get("run")) for f in result["findings"]):
        reasons.append("lint")
    path = _inside_sessions(project, session)
    if path is not None and path.is_file():
        if _day_change(project, path, turn_text):
            reasons.append("day_change")
        if _pending_block(project, _relative(project, path)):
            reasons.append("pending")
    return {"eligible": not reasons, "reasons": reasons}


def _is_log(path: Path) -> bool:
    """A session log or day file: code never writes a state block into one."""
    if path.match(SESSION_GLOB) or path.match(DAY_FILE_GLOB):
        return True
    data = read_frontmatter(path)
    return "scene" in data or data.get("log_day") is not None


def _state_target(project: Path, session_path: Path, data: dict):
    """(target, section, warning): the run state file and section the session names, if code may write it."""
    pointer, warning = _state_pointer(data, _session_layout(session_path)["text"])
    if pointer is None:
        return None, None, warning
    target = _inside_project(project, pointer["raw"])
    if target is None:
        return None, None, f"run state {pointer['raw']} is outside the project or not markdown; nothing written."
    if target == _inside_project(project, INVARIANTS):
        return None, None, f"run state {pointer['raw']} names the invariants; the state block is never written there."
    if not target.is_file():
        return None, None, f"run state {pointer['raw']} does not exist."
    if _is_log(target):
        return None, None, f"run state {pointer['raw']} is a session log; the state block is never written into one."
    return target, pointer["section"], None


def _block_digest(lines: list[str]) -> str:
    return hashlib.sha256("".join(line.rstrip("\r\n") + "\n" for line in lines).encode("utf-8")).hexdigest()[:12]


def _state_block(rel: str, data: dict, today: str) -> list[str]:
    body = ["```yaml\n"] + emit_yaml("run", data["run"]) + ["```\n"]
    beat = str(data.get("shipped") or "").split("@", 1)[0]
    day = fiction_day(data.get("currentDate"))
    as_of = ", ".join(x for x in (f"Beat {beat}" if beat else "", f"Day {day}" if day is not None else "") if x)
    head = (f"{STATE_START} generated from {rel}{' as of ' + as_of if as_of else ''} ({today}). The session "
            f"frontmatter's run: block governs; change it by a delta, never here. sha256:{_block_digest(body)} -->\n")
    return [head] + body + [STATE_END + "\n"]


def _block_lines(plain: list[str], start: int, end: int):
    """(first, last) indices of the state block inside lines[start:end], or None; last is None if unterminated."""
    first = next((i for i in range(start + 1, end) if plain[i].startswith(STATE_START)), None)
    if first is None:
        return None
    return first, next((i for i in range(first + 1, end) if plain[i].strip() == STATE_END), None)


def _state_text(old: str, section: str, block: list[str]) -> str:
    """The run state file with the block as its section's first entry, whether it was there, lower down (a
    prose entry written above it) or absent. Every other line is kept. A block edited by hand is refused."""
    lines = _lines(old)
    plain = [line.rstrip("\r\n") for line in lines]
    nl = _newline(lines)
    bounds = _section_bounds(plain, section)
    if bounds is None:
        raise ValueError(f"the run state file has no '{section}' section")
    start, end = bounds
    found = _block_lines(plain, start, end)
    if found is not None:
        first, last = found
        m = STATE_START_RE.match(plain[first])
        if last is None or m is None or _block_digest(lines[first + 1:last]) != m.group(1):
            raise StateBlockEdited(f"the state block in '{section}' (line {first + 1}) was edited by hand: it is "
                                   "not overwritten. Carry the change in a delta (or the session frontmatter's run: "
                                   "block), then delete the block and run state --refresh to write it again")
        after = last + 1
        if first > 0 and not plain[first - 1].strip() and after < len(lines) and not plain[after].strip():
            after += 1  # one blank line stays where the block was
        lines = lines[:first] + lines[after:]
    rest = lines[start + 1:]
    while rest and not rest[0].strip():
        rest.pop(0)
    heading = lines[start] if lines[start].endswith("\n") else lines[start] + nl
    return "".join(lines[:start] + [heading, nl] + _as_newline(block, nl) + ([nl] if rest else []) + rest)


def refresh_state(project, session_file, today=None) -> dict:
    """Copy the session frontmatter's run: block into the run state file's generated block."""
    project = Path(project)
    today = today or datetime.date.today().isoformat()
    path = Path(session_file)
    path = path if path.is_absolute() else project / path
    data = read_frontmatter(path)
    result = {"action": "none", "path": None, "warnings": []}
    blocked = _pending_block(project, _relative(project, path)) if path.is_file() else None
    if blocked:  # the frontmatter holds an unreviewed beat's delta
        result.update(action="refused", warnings=[blocked])
        return result
    if not isinstance(data.get("run"), dict):
        result["reason"] = "the session has no run: block; its run state is free text"
        return result
    target, section, warning = _state_target(project, path, data)
    if target is None:
        result.update(action="refused", warnings=[warning])
        return result
    result["path"] = _relative(project, target)
    try:
        block = _state_block(_relative(project, path), data, today)
        changed = _replace_text(target, lambda old: _state_text(old, section, block))
    except (ValueError, UnicodeDecodeError) as exc:
        result.update(action="refused", warnings=[str(exc)])
        return result
    result["action"] = "refreshed" if changed else "unchanged"
    return result


def state_block_status(project: Path, path: Path, data: dict):
    """Whether the run state file's block is there, edited by hand, or behind the session's fiction day."""
    if not isinstance(data.get("run"), dict):
        return None
    target, section, warning = _state_target(project, path, data)
    status = {"path": _relative(project, target) if target else None, "present": False, "as_of_day": None,
              "stale": True, "edited": False, "warning": warning}
    lines = (_read_lines(target) or []) if target else []
    bounds = _section_bounds(lines, section) if target else None
    found = _block_lines(lines, *bounds) if bounds else None
    if found:
        first, last = found
        m, day = STATE_START_RE.match(lines[first]), re.search(r"\bDay (\d+)\b", lines[first])
        status.update(present=True, as_of_day=int(day.group(1)) if day else None,
                      edited=last is None or m is None or _block_digest(lines[first + 1:last]) != m.group(1))
        current = fiction_day(data.get("currentDate"))
        status["stale"] = status["as_of_day"] is None or (current is not None and status["as_of_day"] < current)
    return status


def run_report(data: dict, state) -> dict:
    """The session's typed run state, for session: present, malformed entries, overdue clocks."""
    run = data.get("run") if "run" in data else None
    report = {"schema": isinstance(run, dict), "warnings": standing_run_warnings(run),
              "notes": run_notes(run, fiction_day(data.get("currentDate")))}
    run_state = (state or {}).get("run_state")
    if report["schema"] and run_state and run_state.get("current") and not run_state.get("generated"):
        report["warnings"].append(
            f"The current entry of {run_state['path']} § {run_state['section']} (line {run_state['current']['start']})"
            " is hand-written, but this session keeps typed run state: its frontmatter run: block governs. Carry a "
            "change in a delta; state --refresh writes the block and puts it first again, above the prose.")
    return report


def summary_dir(project, data: dict):
    """Work/rp/summaries/<campaign>: the log_dir under Work/rp, without its trailing /log."""
    log_dir, _warning = _log_dir(Path(project), data)
    if log_dir is None:
        return None
    campaign = Path(os.path.relpath(log_dir, SESSION_DIR)).as_posix()
    return f"{SUMMARY_DIR}/{campaign[:-4] if campaign.endswith('/log') else campaign}"


def _day_of(project: Path, rel: str):
    data = read_frontmatter(project / rel)
    return _file_day(data, None if data.get("log_day") is not None else _read_lines(project / rel)), data


def _day_span(project: Path, rel: str, day: int) -> dict:
    """A day's beats in one file, from its first beat working to the end of its last beat, and its Day Plans.

    The span stops before a later day's Day Plan or the next beat's working,
    written early into this file (20 of 25 day boundaries in the logs). In a
    legacy log the day starts where its beats first reach it. Struck drafts
    are left out, as in every read range (parts).
    """
    lines = _read_lines(project / rel) or []
    data = read_frontmatter(project / rel)
    body, stop = _body_start(lines), _footer_start(lines)
    units = _beat_units(lines, body, stop)
    k, start = 0, body
    if data.get("log_day") is None:
        top, k = None, len(units)
        for n, unit in enumerate(units):
            top = unit["day"] if top is None else max(top, unit["day"] or top)
            if top is not None and top >= day:
                k = n
                break
        start = units[k]["cut"] if k < len(units) else stop
    day_units, fenced, beats = units[k:], _fenced(lines), None
    if day_units:
        first = next((i for i in range(start, stop) if i not in fenced and BEAT_HEADING_RE.match(lines[i])), start)
        last = day_units[-1]
        end = next((i for i in range(last["heading"], stop) if i not in fenced and HEADING_RE.match(lines[i])
                    and _goes_forward(lines[i], last["beat"])), stop)
        beats = _skip_struck(_range(rel, lines, first, end), lines)
        if beats:
            top = max((n for u in day_units if (n := _beat_number(u["beat"])) is not None), default=None)
            beats.update(beats=len(day_units), first_beat=day_units[0]["beat"], last_beat=last["beat"], top_beat=top)
    plans = _day_plans(rel, lines, body, start, stop, day)
    if not plans:  # written early, in the file before
        back = _previous_file(project, data, lines)
        back_lines = _read_lines(project / back) if back else None
        if back and back_lines:
            back_stop = _footer_start(back_lines)
            plans = _day_plans(back, back_lines, _body_start(back_lines), back_stop, back_stop, day)
    return {"beats": beats, "plans": plans}


def _closed_days(project: Path, files: list[str]) -> list[dict]:
    """The days rotation closed among files: each closed file's day and where its summary goes."""
    days = []
    for rel in files:
        if not rotation_footer(project / rel):
            continue
        day, data = _day_of(project, rel)
        folder = summary_dir(project, data)
        if day is None or folder is None:
            continue
        path = f"{folder}/{DAY_FILE_NAME.format(day)}"
        days.append({"day": day, "source": rel, "closed": True, "path": path, "exists": (project / path).is_file()})
    return days


def _due_days(project: Path, rel: str, deep: bool = True) -> list[dict]:
    """Closed days behind rel still without a summary, oldest first: a day /rp:start closed lies before the
    session's own files. deep walks back through every file rotation closed (at most MAX_READ_HOPS), so a
    day whose summary failed stays due behind days that have theirs; otherwise the walk stops at the first
    summarised day (session's per-turn view, which costs one hop or two)."""
    due, here, seen = [], rel, {rel}
    for _hop in range(MAX_READ_HOPS):
        data = read_frontmatter(project / here)
        back = _previous_file(project, data, [] if data.get("log_day") is not None
                              else _read_lines(project / here) or [])
        if back is None or back in seen:
            break
        seen.add(back)
        found = _closed_days(project, [back])
        if not found and rotation_footer(project / back):
            here = back  # closed by rotation, but its day or log_dir is unreadable: walk past it
            continue
        if not found:
            break  # the run's first file, or a legacy log /rp:end closed
        if found[0]["exists"] and not deep:
            break
        if not found[0]["exists"]:
            due.append(found[0])
        here = back
    return list(reversed(due))


def _days_due_or_closed(project: Path, files: list[str], deep: bool = True) -> list[dict]:
    """The days the session's files closed, and any earlier closed day still due, in day order."""
    days = {d["source"]: d for d in _closed_days(project, files[:-1])}
    for d in _due_days(project, files[0], deep):
        days.setdefault(d["source"], d)
    return sorted(days.values(), key=lambda d: (d["day"], d["source"]))


def _session_files_for(project: Path, rel: str, opened=None) -> list[str]:
    """The session's files: from the file it opened in (given, else the marker's session_opened) to rel."""
    if opened is None:
        fields = _marker_fields(project)
        named = _inside_sessions(project, str(fields.get("session_file") or ""))
        opened = fields.get("session_opened") if named and _relative(project, named) == rel else None
    return session_files(project, rel, opened)


def summary_plan(project, session_file, opened=None) -> dict:
    """What to summarise: each day this session closed (its beats, plans and summary path) and the open day."""
    project = Path(project)
    rel = _relative(project, project / session_file)
    files = _session_files_for(project, rel, opened)
    days = [dict(d, **_day_span(project, d["source"], d["day"])) for d in _days_due_or_closed(project, files)]
    day, data = _day_of(project, rel)
    span = _day_span(project, rel, day) if day is not None else {"beats": None, "plans": []}
    return {"dir": summary_dir(project, data), "days": days,
            "open": {"day": day, "source": rel, "closed": False, **span}}


def _closed_day_entry(project: Path, rel: str, day: int):
    """The closed file holding a day, walking back from rel through the run, with its span and summary path."""
    here, seen = rel, set()
    while here is not None and here not in seen:  # as far back as the run goes: the plan lists up to
        seen.add(here)                             # MAX_READ_HOPS days, a session up to 500 files
        if here != rel and not rotation_footer(project / here):
            return None  # before the run's day files: a legacy log /rp:end closed
        found = _closed_days(project, [here])
        if found and found[0]["day"] == day:
            return dict(found[0], **_day_span(project, here, day))
        if found and found[0]["day"] < day:
            return None  # walked past it
        data = read_frontmatter(project / here)
        here = _previous_file(project, data, [] if data.get("log_day") is not None
                              else _read_lines(project / here) or [])
    return None


def _strip_frontmatter(text: str) -> str:
    lines = text.splitlines(keepends=True)
    end = _frontmatter_end([line.rstrip("\r\n") for line in lines])
    return "".join(lines[end + 1:]) if end is not None else text


def _beat_number(label) -> int | None:
    m = re.match(r"\d+", str(label or ""))
    return int(m.group(0)) if m else None


def _citation_problems(project: Path, entry: dict, body: str) -> list[str]:
    """A day summary cites only beats up to the day's last, and a line only in a file rotation has closed, never
    one of its struck drafts."""
    problems, struck = [], {}
    beats = entry.get("beats") or {}
    last = beats.get("top_beat") if beats.get("top_beat") is not None else _beat_number(beats.get("last_beat"))
    for m in BEAT_CITE_RE.finditer(body):
        if last is not None and int(m.group(1)) > last:
            problems.append(f"{m.group(0)} comes after the day's last beat, B{last}")
    log_dir, _warning = _log_dir(project, read_frontmatter(project / entry["source"]))
    bases = [project, project / SESSION_DIR] + ([project / log_dir, (project / log_dir).parent] if log_dir else [])
    sessions = (project / SESSION_DIR).resolve()
    for m in LINE_CITE_RE.finditer(body):
        target = next((Path(os.path.normpath(base / m.group(1))) for base in bases if (base / m.group(1)).is_file()),
                      None)
        if target is None or sessions not in target.resolve().parents or not _is_log(target):
            continue  # not a session log (a Lore file, say): no rule here
        a, b = sorted((int(m.group(2)), int(m.group(3) or m.group(2))))  # a range written backwards: the same lines
        if not rotation_footer(target):
            problems.append(f"{m.group(0)} cites a line in {_relative(project, target)}, which is still open (its "
                            "lines move until rotation closes it): cite the beat instead")
        elif b > _line_count(target):
            problems.append(f"{m.group(0)} is past the end of {_relative(project, target)}")
        else:  # an end, past blank lines, on a struck line cites the draft; a range around one is fine
            if target not in struck:
                lines = _read_lines(target) or []
                struck[target] = (lines, struck_spans(lines))
            lines, spans = struck[target]
            while 0 < a < b and not lines[a - 1].strip():
                a += 1
            while b > max(a, 1) and not lines[b - 1].strip():
                b -= 1
            if any(s < n <= e for s, e in spans for n in (a, b)):
                problems.append(f"{m.group(0)} cites a struck draft in {_relative(project, target)}, which is "
                                "never story: cite the beat instead")
    return problems


def _same_or_refuse(target: Path, body: str, what: str) -> dict:
    existing = _strip_frontmatter(target.read_text(encoding="utf-8")).strip("\n")
    if existing == body.strip("\n"):
        return {"action": "unchanged", "path": str(target)}
    raise ValueError(f"{what} already exists and differs; it is never overwritten (correct it by hand)")


def publish_summary(project, session_file, day: int, text_path, today=None) -> dict:
    """Publish a closed day's summary once: the summariser's text, checked, under generated frontmatter."""
    project = Path(project)
    today = today or datetime.date.today().isoformat()
    rel = _relative(project, project / session_file)
    entry = _closed_day_entry(project, rel, int(day))
    if entry is None:
        raise ValueError(f"Day {day} has no file that rotation has closed in this run; a day is summarised once "
                         "it is closed")
    held = provisional_keys(_read_lines(project / entry["source"]))
    if held:
        raise ValueError(f"{entry['source']} holds a provisional beat (key {held[0]}) whose review never settled; "
                         f"settle it before Day {day} is summarised")
    body = _strip_frontmatter(Path(text_path).read_text(encoding="utf-8-sig")).strip("\n") + "\n"
    heading = next((line for line in body.splitlines() if HEADING_RE.match(line)), "")
    m = DAY_HEADING_RE.match(heading)
    if not m or int(m.group(1)) != int(day):
        raise ValueError(f"the summary must open with a '## Day {day}' heading")
    problems = _citation_problems(project, entry, body)
    if problems:
        raise ValueError("; ".join(problems))
    data, beats = read_frontmatter(project / entry["source"]), entry.get("beats") or {}
    span = f"B{beats['first_beat']} to B{beats['last_beat']}" if beats.get("first_beat") else None
    head = ["---\n"]
    for key, value in (("campaign", data.get("campaign")), ("fiction_day", int(day)), ("source", entry["source"]),
                       ("beats", span), ("written", today)):
        if value is not None:
            head += emit_yaml(key, value)
    target = project / entry["path"]
    if target.exists():
        return dict(_same_or_refuse(target, body, entry["path"]), path=entry["path"], day=int(day))
    try:
        _publish(target, "".join(head) + "---\n\n" + body)
    except FileExistsError:  # another summariser got there first
        return dict(_same_or_refuse(target, body, entry["path"]), path=entry["path"], day=int(day))
    return {"action": "published", "path": entry["path"], "day": int(day)}


STATE_AT_CLOSE_RE = re.compile(r"^##\s+State at close\b", re.I)


def _split_days(text: str):
    """(preamble, {day: section}, closing, state at close) of a summary's ## sections; a ## line in a code
    fence starts none. A section between two days stays with the day before it; sections after the last day,
    or all of them when none names a day, close the summary; two sections for one day join. Its own title
    is dropped (stitch writes one); its State at close is returned apart (stitch puts it last)."""
    lines = _lines(_strip_frontmatter(text))
    fenced = _fenced([line.rstrip("\r\n") for line in lines])
    segments, current = [], []
    for i, line in enumerate(lines):
        title = re.match(r"#\s", line) and not segments and not "".join(current).strip()
        if i not in fenced and (line.startswith("## ") or title):
            if "".join(current).strip():
                segments.append("".join(current).strip("\n"))
            current = [] if title else [line]
        else:
            current.append(line)
    if "".join(current).strip():
        segments.append("".join(current).strip("\n"))
    pre, days, post, state, last = [], {}, [], [], None
    marks = [DAY_HEADING_RE.match(seg) for seg in segments]
    any_day = any(marks)
    for n, (seg, mark) in enumerate(zip(segments, marks)):
        if STATE_AT_CLOSE_RE.match(seg):
            state.append(seg)
        elif mark:
            last = int(mark.group(1))
            days.setdefault(last, []).append(seg)
        elif last is None and any_day:
            pre.append(seg)
        elif last is not None and any(marks[n + 1:]):
            days[last].append(seg)
        else:
            post.append(seg)
    return pre, {day: "\n\n".join(parts) for day, parts in days.items()}, post, state


def stitch_summary(project, session_file, out, extra_path=None, today=None, opened=None) -> dict:
    """The session summary: the published day summaries in day order, the extra text's preamble, open-day
    and closing sections around them, and the state at close from the run: block. Published once; the
    same stitch again changes nothing."""
    project = Path(project)
    today = today or datetime.date.today().isoformat()
    target = _inside_sessions(project, str(out))
    if target is None or (project / SUMMARY_DIR).resolve() not in target.resolve().parents:
        raise ValueError(f"{out} must be a .md file under {SUMMARY_DIR}/")
    rel = _relative(project, project / session_file)
    blocked = _pending_block(project, rel)
    if blocked:
        raise ValueError(blocked)
    files = _session_files_for(project, rel, opened)
    closed = _closed_days(project, files[:-1])
    missing = [str(d["day"]) for d in closed if not d["exists"]]
    if missing:
        raise ValueError(f"no summary yet for Day {', '.join(missing)}: publish each closed day's summary first")
    sections = {d["day"]: _strip_frontmatter((project / d["path"]).read_text(encoding="utf-8")).strip("\n")
                for d in closed}
    pre, post, state = [], [], []
    if extra_path:
        pre, extra_days, post, state = _split_days(Path(extra_path).read_text(encoding="utf-8-sig"))
        for day, text in extra_days.items():
            if day in sections:
                raise ValueError(f"Day {day} already has its day summary ({SUMMARY_DIR}/...); the extra text "
                                 "covers only the open day and days no day summary holds")
            sections[day] = text
    data = read_frontmatter(project / rel)
    days = sorted(sections)
    span = (f"Day {days[0]}" + (f" to Day {days[-1]}" if days[-1] != days[0] else "")) if days else None
    head = ["---\n"] + emit_yaml("session_files", files)
    head += emit_yaml("fiction_days", span) if span else []
    head += emit_yaml("day_summaries", [d["path"] for d in closed]) + emit_yaml("stitched", today) + ["---\n\n"]
    campaign = data.get("campaign") if isinstance(data.get("campaign"), str) else None
    title = "# Session summary" + (f": {campaign}" if campaign else "") + (f", {span}" if span else "")
    parts = [title] + pre + [sections[d] for d in days] + post
    state = ["\n\n".join(state)] if state else []  # the GM's: place, gear, what the run: block does not hold
    if isinstance(data.get("run"), dict):
        copy_of = (f"The run: block of `{rel}` as stitched (it governs; this is a copy):\n\n```yaml\n"
                   + "".join(emit_yaml("run", data["run"])) + "```")
        state = [state[0] + "\n\n### Run state\n\n" + copy_of] if state else ["## State at close\n\n" + copy_of]
    parts += state
    body = "\n\n".join(parts) + "\n"
    result = {"action": "published", "path": _relative(project, target), "days": days}
    try:
        if not target.exists():
            _publish(target, "".join(head) + body)
            return result
    except FileExistsError:
        pass
    return dict(result, action=_same_or_refuse(target, body, str(out))["action"])


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
    data = read_frontmatter(project / resolved["path"]) if exists else {}
    resolved["run"] = run_report(data, resolved["live_state"])
    resolved["state_block"] = state_block_status(project, project / resolved["path"], data) if exists else None
    resolved["summaries_due"] = [d["day"] for d in _days_due_or_closed(project, resolved["session_files"], deep=False)
                                 if not d["exists"]] if exists else []  # summary walks the whole run
    resolved["pending"] = pending_reviews(project, resolved["path"]) if exists else []
    for text in resolved["run"]["warnings"]:
        print(f"WARNING: {text}", file=sys.stderr)
    interrupted = _interrupted(project, resolved["path"]) if exists else None
    resolved["rotation_warning"] = (
        f"The rotation from {interrupted} to {resolved['path']} was interrupted before {interrupted} got its "
        "footer; the next rp_gate.py rotate closes it.") if interrupted else None
    if interrupted:
        print(f"WARNING: {resolved['rotation_warning']}", file=sys.stderr)
    lines = _read_lines(project / resolved["path"]) if exists else None
    if lines:
        hidden = {i for s, e in struck_spans(lines) for i in range(s, e)}
        lines = [line for i, line in enumerate(lines) if i not in hidden]
    if lines and "<!-- rp-rotated-to: " in "\n".join(lines[:_footer_start(lines)]):
        print(f"WARNING: a rotation footer appears before the end of {resolved['path']} (quoted, or text was written "
              "after it closed); only a footer that ends a file closes it, so this file counts as open.", file=sys.stderr)
    resolved["resume"] = resume_status(project, resolved) if exists else None
    print(json.dumps(resolved, ensure_ascii=False))
    return 0


def resume_status(project: Path, resolved: dict) -> dict:
    """Whether a fresh session (a Room relaunched at a Day Plan) can take over from here losing nothing: what a
    closing session would still hold that is not yet on disk as settled state, each a reason."""
    reasons, rel = [], resolved["path"]
    if resolved["source"] != "marker":
        reasons.append("no_marker")
    if resolved.get("pending") or provisional_keys(_read_lines(project / rel)):
        reasons.append("pending")
    if (project / QUEUED_INPUT).is_file():
        reasons.append("queued_input")
    if resolved.get("summaries_due"):
        reasons.append("summaries_due")
    if resolved.get("rotation_warning"):
        reasons.append("rotation")
    day = (resolved.get("continuity") or {}).get("day")
    span = _day_span(project, rel, day) if day is not None else {"beats": None, "plans": []}
    return {"ready": not reasons, "reasons": reasons, "day": day,
            "beats_today": (span["beats"] or {}).get("beats", 0), "last_beat": (span["beats"] or {}).get("last_beat"),
            "day_plan": bool(span["plans"])}


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


def cmd_strike(args) -> int:
    project = Path(args.project_dir)
    try:
        text = sys.stdin.read() if args.turn_file == "-" else Path(args.turn_file).read_text(encoding="utf-8")
    except OSError as exc:
        print(f"ERROR: cannot read the turn file: {exc}", file=sys.stderr)
        return 2
    session, warning = _session_for(project, args.session_file)
    if warning:
        print(f"WARNING: {warning}", file=sys.stderr)
    try:
        result = strike(project, session, turn_text=text, attempt=args.attempt, beat=args.beat, reason=args.reason,
                        log=not args.no_log)
    except ValueError as exc:
        print(f"ERROR: {exc}.", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"ERROR: the struck draft was not kept: {exc}. Run the same command again.", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


def _read_arg_file(name):
    return sys.stdin.read() if name == "-" else Path(name).read_text(encoding="utf-8")


def cmd_ship(args) -> int:
    project = Path(args.project_dir)
    session, warning = _session_for(project, args.session_file)
    if warning:
        print(f"WARNING: {warning}", file=sys.stderr)
    try:
        text = _read_arg_file(args.turn_file)
        append = Path(args.append_file).read_text(encoding="utf-8") if args.append_file else None
        result = ship(project, session, turn_text=text, append_text=append, beat=args.beat,
                      config=load_config(project, args.config), log=not args.no_log, provisional=args.provisional,
                      attempt=args.attempt, max_rewrites=args.max_rewrites, reviewed=args.reviewed)
    except (ValueError, UnicodeDecodeError) as exc:
        print(f"ERROR: {exc}.", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"ERROR: ship failed: {exc}. Run the same command again; it ships once.", file=sys.stderr)
        return 2
    for item in result["dropped"]:
        print(f"WARNING: {item['key']} was not applied ({item['check']}).", file=sys.stderr)
    print(json.dumps(result, ensure_ascii=False))
    return 0


def cmd_brief(args) -> int:
    project = Path(args.project_dir)
    session, warning = _session_for(project, args.session_file)
    if warning:
        print(f"WARNING: {warning}", file=sys.stderr)
    try:
        keeper = _read_arg_file(args.input_file)
        report = write_brief(project, session, keeper, config=load_config(project, args.config), budget=args.budget,
                             beat=args.beat)
    except (ValueError, UnicodeDecodeError) as exc:
        print(f"ERROR: {exc}.", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"ERROR: the brief was not written: {exc}. Run it again.", file=sys.stderr)
        return 2
    for text in report["warnings"]:
        print(f"WARNING: {text}", file=sys.stderr)
    print(json.dumps({k: v for k, v in report.items() if k != "live_state"}, ensure_ascii=False))
    return 0


def cmd_settle(args) -> int:
    project = Path(args.project_dir)
    try:
        findings = Path(args.findings_file).read_text(encoding="utf-8") if args.findings_file else None
        correction = Path(args.delta_file).read_text(encoding="utf-8") if args.delta_file else None
        result = settle(project, args.key, verdict=args.verdict, violations=_csv(args.violations), reason=args.reason,
                        findings_text=findings, delta_text=correction, config=load_config(project, args.config),
                        log=not args.no_log)
    except (ValueError, UnicodeDecodeError) as exc:
        print(f"ERROR: {exc}.", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"ERROR: settle failed: {exc}. Run the same command again; it settles once.", file=sys.stderr)
        return 2
    for text in result["warnings"]:
        print(f"WARNING: {text}", file=sys.stderr)
    print(json.dumps(result, ensure_ascii=False))
    return 1 if result["action"] in ("stale", "unshipped", "refused") else 0


def cmd_pending(args) -> int:
    project = Path(args.project_dir)
    session, warning = _session_for(project, args.session_file) if args.session_file else (None, None)
    if warning:
        print(f"WARNING: {warning}", file=sys.stderr)
    found = pending_reviews(project, session)
    started = time.monotonic()
    deadline, keys = started + max(0, min(args.wait or 0, MAX_WAIT)), [e["key"] for e in found]
    while any(e["shipped"] for e in found) and time.monotonic() < deadline:
        time.sleep(1)  # a harness delivers a background result between commands; this gives it one to land on
        found = pending_reviews(project, session)
        if [e["key"] for e in found] != keys:
            break
    waited = round(time.monotonic() - started, 1)
    found = pending_reviews(project, session, detail=True, beats=load_config(project)["log"]["previousBeats"])
    resolved = session or resolve_session_file(project)["path"]
    held = provisional_keys(_read_lines(project / resolved)) if resolved else []
    orphans = [k for k in held if k not in {e["key"] for e in found}]
    cleared = {}
    if args.clear_queued:  # a revised input replaces it; /rp:end reports it
        queued = project / QUEUED_INPUT
        cleared["queued_input"] = queued.read_text(encoding="utf-8") if queued.is_file() else None
        queued.unlink(missing_ok=True)
    if args.clear_carried and resolved and (project / resolved).is_file():  # /rp:end: the run's last findings
        carried = carried_path(project, resolved)
        cleared["carried"] = carried.read_text(encoding="utf-8") if carried.is_file() else None
        carried.unlink(missing_ok=True)
    print(json.dumps({"pending": found, "orphans": [{"session": resolved, "key": k} for k in orphans],
                      "waited_s": waited if args.wait else None, "cleared": cleared or None}, ensure_ascii=False))
    return 0


def cmd_state(args) -> int:
    project = Path(args.project_dir)
    session, warning = _session_for(project, args.session_file)
    if warning:
        print(f"WARNING: {warning}", file=sys.stderr)
    path = project / session
    if args.refresh:
        try:
            result = refresh_state(project, session)
        except OSError as exc:
            print(f"ERROR: the refresh failed: {exc}. Run it again; it writes the block once.", file=sys.stderr)
            return 2
        for text in result["warnings"]:
            print(f"WARNING: {text}", file=sys.stderr)
        print(json.dumps(result, ensure_ascii=False))
        return 1 if result["action"] == "refused" else 0
    data = read_frontmatter(path)
    print(json.dumps({"run": run_report(data, live_state(project, session)),
                      "state_block": state_block_status(project, path, data)}, ensure_ascii=False))
    return 0


def cmd_summary(args) -> int:
    project = Path(args.project_dir)
    session, warning = _session_for(project, args.session_file)
    if warning:
        print(f"WARNING: {warning}", file=sys.stderr)
    try:
        if args.publish:
            if args.day is None:
                raise ValueError("--publish needs --day")
            result = publish_summary(project, session, args.day, args.publish)
        elif args.stitch:
            if not args.out:
                raise ValueError("--stitch needs --out")
            result = stitch_summary(project, session, args.out, args.extra, opened=args.opened)
        else:
            result = summary_plan(project, session, opened=args.opened)
    except (ValueError, UnicodeDecodeError) as exc:
        print(f"ERROR: {exc}.", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"ERROR: {exc}. Run the same command again; it publishes once.", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=None if args.publish or args.stitch else 1))
    return 0


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
    result = lint(turn, scene, config, project, brief=_brief_for(project, turn))
    triggers = beat_triggers(turn, result, scene, config)
    rewrite = args.rewrite or args.attempt > 1  # a later attempt answers a confirmed violation
    decision = route(result, triggers, config, recheck=args.recheck, final=args.final, rewrite=rewrite)
    decision["background"] = background_route(decision, triggers, result, config, project, session, text, rewrite)
    elapsed = round((time.perf_counter() - started) * 1000, 2)
    common = {"session": session, "beat": args.beat, "attempt": args.attempt}
    if not args.no_log:
        append_log(project, {**common, "path": "lint", "findings": [f["check"] for f in result["findings"]],
                             "triggers": triggers, "class": decision["beat"], "next": decision["next"],
                             "recheck": args.recheck or None, "final": args.final or None,
                             "rewrite": rewrite or None, "scene_warning": scene["warning"],
                             "background": (True if decision["background"]["eligible"] else
                                            decision["background"]["reasons"]) if decision["next"] == "review" else None,
                             "invariants_warnings": invariant_kinds or None, "elapsed_ms": elapsed,
                             "opened_sources": result["sources"].get("opened")})
        if decision["next"] == "ship" and not args.final:
            reason = "policy lint-only" if config["review"] == "lint-only" else "routine beat, lint clean"
            append_log(project, {**common, "path": "skipped", "reason": reason})
    output = {
        "session_file": session,
        "session_warning": warning,
        "scene_warning": scene["warning"],
        "lint": {k: result[k] for k in ("findings", "events", "run_notes", "present", "delta", "sources",
                                        "elapsed_ms")},
        "triggers": triggers,
        "route": decision,
        "elapsed_ms": elapsed,
        "invariants": invariants,
        "config_warnings": config["warnings"],
        "logged": not args.no_log,
    }
    _keep_check(project, text, output)
    print(json.dumps(output, ensure_ascii=False, indent=1))
    return 0


CHECK_FILE = "Work/rp/.gate/check.json"  # the last check's output, for a review rerun from a pending review


# ------------------------------------------------------------ the turn file
# /rp:turn writes the gate's turn file from the GM's final message by command,
# never by hand: the five headed blocks, in order, each once. The message may
# open with KEEPER_INPUT, with DRAFT, or with the draft itself before
# GM_SPAWN_LOG; a sentence to the Room before a KEEPER_INPUT or DRAFT header is
# dropped. The Keeper's input comes from the file Step 2 wrote, else from the
# message. A message with no GM block, a block twice or blocks out of order is
# refused and nothing is written: the GM's output is at fault, not the file.

TURN_FILE = "Work/rp/.gate/turn.md"
INPUT_FILE = "Work/rp/.gate/input.md"
GM_BLOCKS = ("GM_SPAWN_LOG", "SOURCE_LOG", "SCENE_STATE_DELTA")


class _NoBlock(ValueError):
    pass


def gm_message_blocks(text: str) -> dict:
    """The GM message's sections and the lines dropped before them, or _NoBlock."""
    lines = YAML_BREAK_RE.split(text)
    heads = [(i, m.group(1), (m.group(2) or "").strip())
             for i, m in ((i, SECTION_RE.match(line)) for i, line in enumerate(lines))
             if m]
    names = [name for _i, name, _rest in heads]
    for name in SECTIONS:
        if names.count(name) > 1:
            raise _NoBlock(f"the message carries {names.count(name)} {name} blocks; one is expected")
    missing = [name for name in GM_BLOCKS if name not in names]
    if missing:
        raise _NoBlock(f"no GM block: the message has no {', '.join(missing)} header")
    if names != [name for name in SECTIONS if name in names]:
        raise _NoBlock(f"the GM blocks are out of order: {', '.join(names)}")
    sections = {}
    for n, (i, name, rest) in enumerate(heads):
        stop = heads[n + 1][0] if n + 1 < len(heads) else len(lines)
        body = ([rest] if rest not in ("", "|", ">") else []) + lines[i + 1:stop]
        sections[name] = "\n".join(body).strip("\n").rstrip()
    first = heads[0][0]
    if heads[0][1] in ("KEEPER_INPUT", "DRAFT"):
        dropped = sum(1 for line in lines[:first] if line.strip())
    else:  # the draft itself opens the message
        sections["DRAFT"], dropped = "\n".join(lines[:first]).strip("\n").rstrip(), 0
    if not sections.get("DRAFT", "").strip():
        raise _NoBlock("the draft is empty")
    return {"sections": sections, "dropped": dropped}


def claude_messages(text: str) -> list[str]:
    """Claude Code only: the texts a task's JSONL hands back, in order: each assistant message's text blocks, the
    string `message` of any tool call it makes (the GM's handback), and a stream's type: result line. The GM's message
    is the last of them that carries its blocks: a late alert can wake it after the handback ("Nothing left to do")."""
    found = []
    for raw in text.splitlines():
        if not raw.strip():
            continue
        try:
            entry = json.loads(raw)
        except ValueError:
            raise _NoBlock("not JSONL: a line is not JSON") from None
        if not isinstance(entry, dict):
            continue
        if entry.get("type") == "result" and isinstance(entry.get("result"), str):
            found.append(entry["result"])
        message = entry.get("message") if isinstance(entry.get("message"), dict) else {}
        if entry.get("type") == "assistant" or message.get("role") == "assistant":
            content = message.get("content")
            blocks = [{"type": "text", "text": content}] if isinstance(content, str) else (
                content if isinstance(content, list) else [])
            texts = [b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text"]
            joined = "\n".join(part for part in texts if part).strip()
            if joined:
                found.append(joined)
            for b in blocks:
                if isinstance(b, dict) and b.get("type") == "tool_use" and isinstance(b.get("input"), dict):
                    handed = b["input"].get("message")
                    if isinstance(handed, str) and handed.strip():
                        found.append(handed)
    if not found:
        raise _NoBlock("no assistant text in the JSONL")
    return found


def cmd_turn(args) -> int:
    project = Path(args.project_dir)  # the files are read and written relative to it
    try:
        text = sys.stdin.read() if args.from_file == "-" else (project / args.from_file).read_text(encoding="utf-8",
                                                                                                     errors="replace")
        keeper = (project / args.input_file).read_text(encoding="utf-8", errors="replace") if args.input_file else None
    except OSError as exc:
        print(f"ERROR: cannot read: {exc}", file=sys.stderr)
        return 2
    try:
        if args.format == "claude-jsonl":
            errors = []
            for candidate in reversed(claude_messages(text)):
                try:
                    found = gm_message_blocks(candidate)
                    break
                except _NoBlock as exc:
                    errors.append(exc)
            else:
                raise _NoBlock(f"no GM block in the JSONL's messages (the last one: {errors[0]})")
        else:
            found = gm_message_blocks(text)
    except _NoBlock as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    sections, warnings = found["sections"], []
    if keeper is not None and not keeper.strip():
        print(f"ERROR: the input file {args.input_file} is empty", file=sys.stderr)
        return 2
    if keeper is not None:
        given = sections.get("KEEPER_INPUT", "")
        if given.strip() and " ".join(given.split()) != " ".join(keeper.split()):
            warnings.append("the message's KEEPER_INPUT differs from --input-file; the file is written")
        sections["KEEPER_INPUT"], source = keeper.strip("\n").rstrip(), "file"
    elif sections.get("KEEPER_INPUT", "").strip():
        source = "message"
    else:
        print("ERROR: no KEEPER_INPUT: the message has none; pass --input-file", file=sys.stderr)
        return 2
    out = project / (args.out or TURN_FILE)
    body = "\n\n".join(f"{name}:\n{sections.get(name, '')}" for name in SECTIONS) + "\n"
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        _write_atomic(out, body.encode("utf-8"))
    except OSError as exc:
        print(f"ERROR: cannot write {out}: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"path": str(out), "format": args.format, "keeper_input": source,
                      "sections": {name: len(sections.get(name, "").splitlines()) for name in SECTIONS},
                      "dropped_before": found["dropped"], "warnings": warnings}, ensure_ascii=False, indent=1))
    return 0


def _turn_digest(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


def _keep_check(project: Path, text: str, output: dict) -> None:
    try:
        path = project / CHECK_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_atomic(path, json.dumps({"turn": _turn_digest(text), **output}, ensure_ascii=False).encode("utf-8"))
    except OSError:
        pass  # scratch: a check never fails for it


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
    u = sub.add_parser("turn", help="write the gate's turn file from the GM's final message: its five blocks, in order")
    u.add_argument("--project-dir", default=".")
    u.add_argument("--from", dest="from_file", required=True, help="the GM's final message as text, or - for stdin")
    u.add_argument("--format", choices=("text", "claude-jsonl"), default="text",
                   help="claude-jsonl (Claude Code only): a task's JSONL; its final assistant text is the message")
    u.add_argument("--input-file", help=f"the Keeper's input as Step 2 wrote it ({INPUT_FILE}), written as KEEPER_INPUT")
    u.add_argument("--out", help=f"the turn file to write (default {TURN_FILE})")
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
    r.add_argument("--repair", action="store_true",
                   help="/rp:end: only close a file an interrupted rotation left open, and write struck drafts waiting")
    r.add_argument("--allow-jump", action="store_true",
                   help=f"rotate past more than {MAX_DAY_JUMP} days: only on the Keeper's word")
    r.add_argument("--config", help=f"mode file (default: {MODE_FILE})")
    r.add_argument("--no-log", action="store_true")
    k = sub.add_parser("strike", help="keep a draft the gate rejected, for rotate to write as a STRUCK block")
    k.add_argument("--project-dir", default=".")
    k.add_argument("--session-file", help="the live session file (default: from the marker)")
    k.add_argument("--turn-file", required=True, help="the turn file the gate checked, or - for stdin")
    k.add_argument("--attempt", type=int, required=True, help="the attempt number of the struck draft")
    k.add_argument("--beat", help="the beat label")
    k.add_argument("--reason", required=True, help="the verdict's violation categories, or one line")
    k.add_argument("--no-log", action="store_true")
    p = sub.add_parser("split", help="plan, read-only, how existing logs would cut into day files")
    p.add_argument("--project-dir", default=".")
    p.add_argument("--log", action="append", required=True, help="a log to plan, in play order (repeatable)")
    p.add_argument("--log-dir", required=True, help=f"the day-file directory, e.g. {SESSION_DIR}/<campaign>/log")
    p.add_argument("--cite", action="append", help="a file or directory to scan for citations (repeatable)")
    h = sub.add_parser("ship", help="after rotate: append the beat and apply its gated delta, in one write")
    h.add_argument("--project-dir", default=".")
    h.add_argument("--session-file", help="the file rotate named (default: from the marker)")
    h.add_argument("--turn-file", required=True, help="the gated turn file, or - for stdin; its delta is applied")
    h.add_argument("--append-file", help="the text to append: the new day's Day Plan, if any, then the beat")
    h.add_argument("--beat", help="the beat label; with the text and the delta it makes a retry a no-op")
    h.add_argument("--config", help=f"mode file (default: {MODE_FILE})")
    h.add_argument("--no-log", action="store_true")
    h.add_argument("--provisional", action="store_true",
                   help="ship a first draft before its review (route.background eligible); settle applies the verdict")
    h.add_argument("--attempt", type=int, default=1, help="the attempt being shipped (provisional: 1 only)")
    h.add_argument("--max-rewrites", type=int, help="route.max_rewrites (provisional: required), kept for a redraft")
    h.add_argument("--reviewed", action="store_true",
                   help="the reviewer passed this beat: it consumes the findings a soft background review carried")
    e = sub.add_parser("settle", help="apply a background review's verdict to the provisional beat it reviewed")
    e.add_argument("--project-dir", default=".")
    e.add_argument("--key", required=True, help="the key ship --provisional printed (the verdict's review_key)")
    e.add_argument("--verdict", required=True, choices=VERDICTS,
                   help="clean, soft (findings carried to the next draft) or struck (a confirmed hard finding)")
    e.add_argument("--violations", help="the verdict's categories, comma separated")
    e.add_argument("--reason", help="one line for the struck callout")
    e.add_argument("--findings-file", help="soft: the findings, verbatim, to carry to the next draft")
    e.add_argument("--delta-file", help="soft: a SCENE_STATE_DELTA correcting the state the findings name, applied "
                                        "to the frontmatter in the same write")
    e.add_argument("--config", help=f"mode file (default: {MODE_FILE})")
    e.add_argument("--no-log", action="store_true")
    b = sub.add_parser("brief", help="build this turn's GM brief from disk: state, Day Plan, beats, keyed entries")
    b.add_argument("--project-dir", default=".")
    b.add_argument("--session-file", help="the live session file (default: from the marker)")
    b.add_argument("--input-file", required=True, help="the Keeper's input, verbatim, or - for stdin")
    b.add_argument("--beat", help="the label of the beat being drafted (default: the last beat's number + 1)")
    b.add_argument("--budget", type=int, help="the brief's size in bytes (default: turnGate.briefBytes)")
    b.add_argument("--config", help=f"mode file (default: {MODE_FILE})")
    n = sub.add_parser("pending", help="the provisional beats whose review has not settled")
    n.add_argument("--project-dir", default=".")
    n.add_argument("--session-file", help="only this session file's (default: every pending review)")
    n.add_argument("--wait", type=int, help=f"sleep up to this many seconds (at most {MAX_WAIT}) while a review is "
                                            "pending, so the harness can deliver a background result")
    n.add_argument("--clear-queued", action="store_true", help="remove the held Keeper input, printing it")
    n.add_argument("--clear-carried", action="store_true", help="remove this run's carried findings, printing them")
    t = sub.add_parser("state", help="the run: block's health; --refresh copies it into the run state file")
    t.add_argument("--project-dir", default=".")
    t.add_argument("--session-file", help="the live session file (default: from the marker)")
    t.add_argument("--refresh", action="store_true", help="write the generated block (after a rotation, /rp:end)")
    m = sub.add_parser("summary", help="per-day summaries: plan, --publish a closed day's, or --stitch the session's")
    m.add_argument("--project-dir", default=".")
    m.add_argument("--session-file", help="the live session file (default: from the marker)")
    m.add_argument("--day", type=int, help="the closed fiction day to --publish")
    m.add_argument("--publish", help="a file holding the day's summary text, opening with '## Day N'")
    m.add_argument("--stitch", action="store_true", help="write the session summary from the day summaries")
    m.add_argument("--out", help=f"the session summary to write, under {SUMMARY_DIR}/ (--stitch)")
    m.add_argument("--extra", help="text for --stitch: the session's own sections and the open day's")
    m.add_argument("--opened", help="the file the session opened in (default: the marker's session_opened); "
                                    "/rp:end passes it, having removed the marker")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0) if exc.code not in (None, 0) else 0
    commands = {"session": cmd_session, "turn": cmd_turn, "check": cmd_check, "log": cmd_log, "invariants": cmd_invariants,
                "rotate": cmd_rotate, "strike": cmd_strike, "split": cmd_split, "ship": cmd_ship,
                "state": cmd_state, "summary": cmd_summary, "settle": cmd_settle, "pending": cmd_pending,
                "brief": cmd_brief}
    return commands[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
