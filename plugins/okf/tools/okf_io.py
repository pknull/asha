#!/usr/bin/env python3
"""Dependency-free helpers shared by Asha's OKF tools.

The indexer and log writer run on the SessionStart path through the learnings
manager, which must not depend on PyYAML. Frontmatter scalars are therefore read
with JSON first (Asha's own records are JSON-in-YAML), PyYAML when it happens
to be installed, and a line scraper for plain `key: value` scalars otherwise.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

FRONTMATTER_RE = re.compile(r"^﻿?---[ \t]*\r?\n(?:(.*?)\r?\n)?---[ \t]*(?:\r?\n|$)", re.DOTALL)
_SCALAR_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*):[ \t]*(.*?)[ \t]*$")
RESERVED = ("index.md", "log.md")


def atomic_write(path: Path, text: str) -> None:
    """Write text to path through a same-directory temp file and os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.tmp.", dir=path.parent)
    tmp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def split_frontmatter(text: str) -> tuple[str | None, str]:
    """Return (frontmatter_text, body); frontmatter_text is None when absent."""
    match = FRONTMATTER_RE.match(text)
    if not match:
        return None, text
    return match.group(1) or "", text[match.end():]


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        inner = value[1:-1]
        if value[0] == '"':
            try:
                return json.loads(value)
            except ValueError:
                return inner
        return inner
    return value


def read_scalars(text: str) -> dict:
    """Best-effort frontmatter mapping: JSON, then PyYAML, then top-level scalars."""
    block, _ = split_frontmatter(text)
    if block is None:
        return {}
    stripped = block.strip()
    if stripped.startswith("{"):
        try:
            data = json.loads(stripped)
            return data if isinstance(data, dict) else {}
        except ValueError:
            pass
    try:
        import yaml  # type: ignore
    except ImportError:
        yaml = None
    if yaml is not None:
        try:
            data = yaml.safe_load(block)
            return data if isinstance(data, dict) else {}
        except Exception:  # pragma: no cover - malformed YAML falls through
            pass
    data: dict = {}
    for line in block.splitlines():
        match = _SCALAR_RE.match(line)
        if match and match.group(2) and not match.group(2).startswith(("[", "{", "|", ">")):
            data[match.group(1)] = _unquote(match.group(2))
    return data


def is_excluded(rel: str, excludes) -> bool:
    """fnmatch against the bundle-relative posix path; a trailing slash means a prefix."""
    from fnmatch import fnmatch
    for pattern in excludes:
        pattern = pattern.strip()
        if not pattern:
            continue
        if pattern.endswith("/"):
            if rel == pattern[:-1] or rel.startswith(pattern):
                return True
            continue
        if fnmatch(rel, pattern) or fnmatch(rel, pattern + "/*") or rel.startswith(pattern + "/"):
            return True
    return False


def iter_markdown(root: Path, excludes=()) -> list[Path]:
    """Every .md file under root, hidden directories and excluded paths skipped."""
    files = []
    for path in sorted(root.rglob("*.md")):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if any(part.startswith(".") for part in rel.parts[:-1]):
            continue
        if is_excluded(rel.as_posix(), excludes):
            continue
        files.append(path)
    return files
