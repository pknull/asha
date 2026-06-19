#!/usr/bin/env python3
# ---------------------------------------------------------------------------
# Vendored from https://github.com/sniperunder123/okf-knowledge (MIT, @4d4a0f2)
# OKF v0.1 tooling. Do not edit locally; re-vendor from upstream to update.
# ---------------------------------------------------------------------------
"""Shared helpers for the Open Knowledge Format (OKF) tooling.

Frontmatter parsing, code stripping, and link extraction used by the validator
(and available to other OKF scripts). Requires PyYAML for authoritative YAML
parsing:  pip install pyyaml
"""
from __future__ import annotations

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
