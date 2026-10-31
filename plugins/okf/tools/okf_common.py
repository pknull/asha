#!/usr/bin/env python3
# ---------------------------------------------------------------------------
# Vendored from https://github.com/sniperunder123/okf-knowledge (MIT)
# commit cbfb00bc42be73c0756b243029c783ad31406f60 (2026-09-17), OKF v0.2 tooling.
# upstream-sha256: 1400b7a89af9404c424a32a45cc1abaf5b38743ade9e60482a9288af7fea6858
# Do not edit locally; re-vendor from upstream (plugins/okf/README.md).
# ---------------------------------------------------------------------------
"""Shared helpers for the Open Knowledge Format (OKF) tooling.

Frontmatter parsing, code stripping, and link extraction used by the validator
(and available to other OKF scripts). Requires PyYAML for authoritative YAML
parsing:  pip install pyyaml
"""
from __future__ import annotations

import datetime
import re

try:
    import yaml
except ImportError:  # pragma: no cover - surfaced by the caller
    yaml = None

# Opening "---" (optional BOM), an optional body, then a closing "---" line.
# Tolerates a leading UTF-8 BOM and CRLF line endings.
FRONTMATTER_RE = re.compile(
    r"^﻿?---[ \t]*\r?\n(?:(.*?)\r?\n)?---[ \t]*(?:\r?\n|$)", re.DOTALL
)

# Fenced code blocks: ``` or ~~~ (>= 3) with an optional info string, up to the
# matching closing fence (or end of document if unterminated).
_FENCE_RE = re.compile(
    r"^[ \t]*(`{3,}|~{3,})[^\n]*\n.*?(?:^[ \t]*\1[ \t]*$|\Z)",
    re.DOTALL | re.MULTILINE,
)
# Inline code spans: a run of N backticks, the shortest content, then N backticks.
_INLINE_CODE_RE = re.compile(r"(`+)(?:.+?)\1", re.DOTALL)

# Inline links: [text](target).  Target stops at whitespace or ')'.
_INLINE_LINK_RE = re.compile(r"\[[^\]]*\]\(\s*([^)\s]+)")
# Reference definitions at line start: [id]: target
_REF_DEF_RE = re.compile(r"^[ \t]*\[[^\]]+\]:[ \t]*(\S+)", re.MULTILINE)
# Autolinks: <scheme:target> (CommonMark requires a URI scheme, e.g.
# <https://...> or <mailto:x@y>). Requiring "scheme:" avoids capturing plain
# HTML tags such as <a>, </a>, or <br/> as if they were links.
_AUTOLINK_RE = re.compile(r"<([A-Za-z][A-Za-z0-9+.\-]*:[^>\s]+)>")
# HTML href attributes: href="target" or href='target'
_HREF_RE = re.compile(r"""href\s*=\s*["']([^"']+)["']""", re.IGNORECASE)


def parse_frontmatter(text):
    """Return (data, error). `data` is a dict on success, None on failure."""
    m = FRONTMATTER_RE.match(text)
    if not m:
        if text.lstrip().startswith("---"):
            return None, "frontmatter block is not terminated by a closing '---'"
        return None, "missing YAML frontmatter block (file must start with '---')"
    try:
        data = yaml.safe_load(m.group(1) or "")
    except yaml.YAMLError as exc:
        return None, f"frontmatter is not valid YAML: {str(exc).splitlines()[0]}"
    if data is None:
        return {}, None
    if not isinstance(data, dict):
        return None, "frontmatter is not a YAML mapping"
    return data, None


def strip_code(text):
    """Remove fenced ``` code blocks AND inline `code` spans from `text`.

    Fenced blocks are removed first so backtick-quoted identifiers inside a
    fence (e.g. `acme.orders` in a SQL block) cannot confuse inline-span
    pairing.  Each removed region is replaced with a single newline so line
    boundaries (used by reference-definition matching) are preserved.
    """
    text = _FENCE_RE.sub("\n", text)
    text = _INLINE_CODE_RE.sub(" ", text)
    return text


def extract_links(text):
    """Return target strings found in `text`, AFTER stripping code.

    Covers inline [text](target), reference definitions `[id]: target` at line
    start, autolinks <target>, and HTML href="target".  Links inside fenced or
    inline code are ignored because they are stripped first.
    """
    stripped = strip_code(text)
    targets = []
    for pat in (_INLINE_LINK_RE, _REF_DEF_RE, _AUTOLINK_RE, _HREF_RE):
        for target in pat.findall(stripped):
            target = target.strip()
            if target:
                targets.append(target)
    return targets


def is_external(target):
    return "://" in target or target.startswith("mailto:")


def path_part(target):
    return target.split("#", 1)[0].split("?", 1)[0]


def resolve_link(part, md_file, root):
    if part.startswith("/"):
        return root / part.lstrip("/")
    return md_file.parent / part


def is_nonempty_string(value):
    return isinstance(value, str) and value.strip() != ""


# ---------------------------------------------------------------------------
# OKF v0.2 helpers: trust (`generated`/`verified`) and lifecycle (`stale_after`).
# Shared by validate.py (lints) and visualize.py (styling) so both agree.
# ---------------------------------------------------------------------------

def parse_iso8601(value):
    """Return a `datetime` for an ISO 8601 value, or None if unparseable.

    PyYAML resolves an unquoted ISO 8601 scalar to a native `datetime.date` /
    `datetime.datetime` at parse time, so `value` may already be one of those
    (accepted as-is) rather than the YAML source string.
    """
    if isinstance(value, datetime.datetime):
        return value
    if isinstance(value, datetime.date):
        return datetime.datetime(value.year, value.month, value.day)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.datetime.fromisoformat(text)
    except ValueError:
        return None


def normalize_verified(data):
    """Return frontmatter `verified` as a list of {by, at} mappings.

    A bare `{by, at}` mapping counts as a one-element list (OKF v0.2 §5.2).
    """
    verified = data.get("verified") if isinstance(data, dict) else None
    if isinstance(verified, dict):
        return [verified]
    if isinstance(verified, list):
        return [v for v in verified if isinstance(v, dict)]
    return []


def actor_is_human(actor):
    return isinstance(actor, str) and actor.startswith("human:")


def trust_tier(verified_list):
    """Derive a trust tier from a normalized `verified` list (OKF v0.2 §5.3)."""
    if not verified_list:
        return "unverified"
    if any(actor_is_human(v.get("by")) for v in verified_list):
        return "human-reviewed"
    return "machine-confirmed"


def is_stale(stale_after, now=None):
    """True if `now` is at or after `stale_after` (OKF v0.2 §5.5)."""
    dt = parse_iso8601(stale_after)
    if dt is None:
        return False
    now = now or datetime.datetime.now(datetime.timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=datetime.timezone.utc)
    return now >= dt
