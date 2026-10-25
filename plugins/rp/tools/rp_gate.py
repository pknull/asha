#!/usr/bin/env python3
"""Per-beat RP continuity gate: session resolution, beat lint, routing, logging.

Standard library only and no model calls. /rp:turn runs `check` on every
draft; it decides whether a model reviewer is worth spending on the beat.

  session     print the live session file (marker first, never the clock), where
              its live state is, and any invariants warning
  check       lint one GM turn, route it, append the lint (and skipped) lines
  log         append one review, adjudicate or turn line to the validator log
  invariants  report live state in Memory/invariants.md and its staleness

The lint checks only what can be checked mechanically: the closing
Scene/Location/Present anchor, named actors against the scene's people, and
SCENE_STATE_DELTA keys and their agreement with the anchor. Everything that
needs judgement belongs to the continuity reviewer.

Current state is never read from the invariants: it lives in the session's
frontmatter and header and in the run's state file, which the session names.
"""
from __future__ import annotations

import argparse
import copy
import datetime
import glob
import itertools
import json
import os
import re
import sys
import time
from pathlib import Path

SESSION_DIR = "Work/rp"
SESSION_GLOB = "rp_session_*.md"
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


def _marker_session(project: Path):
    try:
        text = (project / MARKER).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        key, rest = _split_key(line.strip())
        if key == "session_file" and rest:
            value = _scalar(rest)
            return value if isinstance(value, str) and value else None
    return None


def _inside_sessions(project: Path, name: str):
    candidate = (project / name).resolve()
    sessions = (project / SESSION_DIR).resolve()
    if candidate.suffix != ".md" or sessions not in candidate.parents:
        return None
    return Path(os.path.normpath(project / name))


def _relative(project: Path, path: Path) -> str:
    return Path(os.path.relpath(path, project)).as_posix()


def resolve_session_file(project, today: str | None = None) -> dict:
    """The live session file: the marker's record, else the newest open session.

    The clock never names it: one session can run for days.
    """
    project = Path(project)
    today = today or datetime.date.today().isoformat()
    warning = ""
    named = _marker_session(project)
    if named:
        path = _inside_sessions(project, named)
        if path and path.is_file():
            return {"path": _relative(project, path), "source": "marker", "warning": None}
        warning = f"rp-active names {named}, which is not a session file under {SESSION_DIR}/. "
    else:
        warning = "rp-active does not name the session file. "
    sessions = sorted((project / SESSION_DIR).glob(SESSION_GLOB), key=lambda p: p.name, reverse=True)
    for path in sessions:
        if str(read_frontmatter(path).get("status", "")).lower() != "closed":
            rel = _relative(project, path)
            return {
                "path": rel,
                "source": "latest",
                "warning": warning + f"Using the newest open session, {rel}. /rp:start records it in the marker.",
            }
    rel = f"{SESSION_DIR}/rp_session_{today}.md"
    return {"path": rel, "source": "new", "warning": warning + f"No session file exists yet; a new one would be {rel}."}


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
    if dates:
        state_date, source = max(dates), "run_state"
    elif started and _iso(started.group(1)) and started.group(1) <= today:
        state_date, source = started.group(1), "session"
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
    delta_text = turn.get("SCENE_STATE_DELTA", "").strip()
    delta = {}
    if delta_text and delta_text.lower().strip(" .()") not in ("none", "null", "{}", "-", "n/a", "no change",
                                                                  "no changes", "unchanged"):
        data = parse_yaml(delta_text)
        if isinstance(data, dict):
            for key, value in data.items():
                if key == "scene" and isinstance(value, dict):
                    delta.update({f"scene.{k}": v for k, v in value.items()})
                else:
                    delta[key] = value
        else:
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


def load_config(project, path=None) -> dict:
    """The turnGate block of the project's rp mode file, over the defaults."""
    config = copy.deepcopy(DEFAULTS)
    warnings = []
    mode = Path(path) if path else Path(project) / MODE_FILE
    block = None
    if mode.is_file():
        text = _top_level_block(mode.read_text(encoding="utf-8", errors="replace"), "turnGate")
        if text:
            parsed = parse_yaml(text)
            block = parsed.get("turnGate") if isinstance(parsed, dict) else None
            if block is not None and not isinstance(block, dict):
                warnings.append("turnGate is not a mapping; using defaults.")
                block = None
    block = block or {}
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
    resolved = resolve_session_file(project)
    if resolved["warning"]:
        print(f"WARNING: {resolved['warning']}", file=sys.stderr)
    config = load_config(project, args.config)
    resolved["gate"] = {k: config[k] for k in ("review", "rewrites", "models", "warnings")}
    resolved["live_state"], resolved["invariants"] = _state_reports(project, resolved["path"],
                                                                    resolved["source"] != "new")
    _warn_all(resolved["live_state"], resolved["invariants"])
    print(json.dumps(resolved, ensure_ascii=False))
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
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0) if exc.code not in (None, 0) else 0
    commands = {"session": cmd_session, "check": cmd_check, "log": cmd_log, "invariants": cmd_invariants}
    return commands[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
