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
    """Write text to path through a same-directory temp file and os.replace.

    A symlinked target (a stow link into a dotfiles tree) is written through:
    os.replace on the link itself would turn it into a regular file and detach
    it from the tree that tracks it.
    """
    path = Path(path)
    if path.is_symlink():
        path = path.resolve()
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
    rel = rel.rstrip("/")
    for pattern in excludes:
        pattern = pattern.strip()
        if not pattern:
            continue
        base = pattern.rstrip("/")
        # A pattern names a path or a glob; with or without the trailing slash it
        # also covers everything beneath a matching directory.
        if fnmatch(rel, base) or fnmatch(rel, base + "/*"):
            return True
    return False


IGNORE_FILE = ".okfignore"
CONFIG_FILE = ".okfconfig"


def load_config(root: Path) -> dict:
    """The bundle's .okfconfig: a small JSON mapping, {} when absent or malformed."""
    path = Path(root) / CONFIG_FILE
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def lint_waivers(root: Path) -> list[str]:
    """Field-lint prefixes the bundle waives, from .okfconfig {"waive": ["status"]}.

    A bundle whose own taxonomy owns a word OKF also uses (the AAS vault's
    `status` ladder) keeps its vocabulary; the spec makes these producer lints,
    never errors, and consumers must tolerate unknown values. Waivers are
    printed with every verdict so a pass never hides them.
    """
    value = load_config(root).get("waive")
    if not isinstance(value, list):
        return []
    return [str(v) for v in value if isinstance(v, str) and v.strip()]


def link_grammar(root: Path, override: str | None = None) -> str:
    """'standard' unless .okfconfig says {"links": "obsidian"} or the caller overrides."""
    if override:
        return override
    value = load_config(root).get("links")
    return value if value in ("standard", "obsidian") else "standard"


def load_ignore(root: Path) -> list[str]:
    """Patterns from the bundle's .okfignore: one per line, # comments, blanks skipped.

    The same syntax as --exclude (fnmatch on the bundle-relative path; a
    trailing slash means a directory prefix), so one file scopes a bundle for
    every tool that reads it.
    """
    path = Path(root) / IGNORE_FILE
    if not path.is_file():
        return []
    patterns = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            patterns.append(line)
    return patterns


def effective_excludes(root: Path, excludes=(), use_ignore: bool = True) -> list[str]:
    merged = list(load_ignore(root)) if use_ignore else []
    merged.extend(p for p in excludes if p not in merged)
    return merged


def iter_markdown(root: Path, excludes=()) -> list[Path]:
    """Every .md file under root, following directory symlinks once each.

    Hidden directories and excluded paths are skipped. A bundle root such as
    ~/.asha reaches its directories through symlinks into a dotfiles tree, and
    Path.rglob does not descend those, so this walks with os.walk(followlinks)
    and a realpath guard against cycles.
    """
    root = Path(root)
    files: list[Path] = []
    seen_dirs = {os.path.realpath(root)}
    for current, dirnames, filenames in os.walk(root, followlinks=True):
        current_path = Path(current)
        rel_dir = current_path.relative_to(root)
        keep = []
        for name in sorted(dirnames):
            if name.startswith("."):
                continue
            rel = (rel_dir / name).as_posix()
            if is_excluded(rel, excludes) or is_excluded(rel + "/", excludes):
                continue
            real = os.path.realpath(current_path / name)
            if real in seen_dirs:
                continue
            seen_dirs.add(real)
            keep.append(name)
        dirnames[:] = keep
        for name in sorted(filenames):
            if not name.endswith(".md"):
                continue
            path = current_path / name
            if not path.is_file():
                continue
            rel = (rel_dir / name).as_posix()
            if is_excluded(rel, excludes):
                continue
            files.append(path)
    return sorted(files, key=lambda p: p.relative_to(root).as_posix())
