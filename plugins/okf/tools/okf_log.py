#!/usr/bin/env python3
"""Append entries to an OKF bundle's log.md (OKF v0.2 §9).

The log is flat and date-grouped, newest first, with `## YYYY-MM-DD` headings
and no frontmatter. Entries are prose lines; the caller supplies the leading
bold keyword (Creation, Update, Deprecation, Fix, ...). Within a day the newest
entry is first as well.

Usage:
    okf_log.py BUNDLE --message TEXT [--actor ACTOR] [--date YYYY-MM-DD]
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import date
from pathlib import Path

try:
    from okf_io import atomic_write
except ImportError:  # run as a script from another cwd
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from okf_io import atomic_write

DATE_HEADING_RE = re.compile(r"^(#{1,6})\s+(\d{4}-\d{2}-\d{2})\s*$")
TITLE = "# Log"


def parse_log(text: str) -> tuple[list[str], dict[str, list[str]]]:
    """Return (preamble lines, {date: entry lines}) preserving entry order."""
    preamble: list[str] = []
    groups: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        match = DATE_HEADING_RE.match(line)
        if match:
            current = match.group(2)
            groups.setdefault(current, [])
            continue
        if current is None:
            preamble.append(line)
        elif line.strip():
            groups[current].append(line.rstrip())
    return preamble, groups


def render_log(groups: dict[str, list[str]]) -> str:
    out = [TITLE, ""]
    for day in sorted(groups, reverse=True):
        entries = groups[day]
        if not entries:
            continue
        out.append(f"## {day}")
        out.append("")
        out.extend(entries)
        out.append("")
    return "\n".join(out).rstrip("\n") + "\n"


def append_entry(root: Path, message: str, actor: str | None = None, day: str | None = None) -> Path:
    path = Path(root) / "log.md"
    message = " ".join(message.split())
    if not message:
        raise ValueError("log entry message must not be empty")
    entry = f"- {message}" + (f" ({actor})" if actor else "")
    groups: dict[str, list[str]] = {}
    if path.is_file():
        _, groups = parse_log(path.read_text(encoding="utf-8"))
    today = day or date.today().isoformat()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", today):
        raise ValueError(f"log date must be YYYY-MM-DD: {today!r}")
    groups.setdefault(today, []).insert(0, entry)
    atomic_write(path, render_log(groups))
    return path


def order_problems(text: str) -> list[str]:
    """Lints for a log: non-descending date headings or a frontmatter block."""
    problems = []
    if text.lstrip("﻿").startswith("---"):
        problems.append("log.md must not carry a frontmatter block")
    previous = None
    for line in text.splitlines():
        match = DATE_HEADING_RE.match(line)
        if not match:
            continue
        day = match.group(2)
        if previous is not None and day >= previous:
            problems.append(f"log.md date heading {day} is not newer-first after {previous}")
        previous = day
    return problems


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Append an entry to an OKF bundle log.md.")
    parser.add_argument("bundle")
    parser.add_argument("--message", required=True)
    parser.add_argument("--actor", default=None)
    parser.add_argument("--date", default=None, help="YYYY-MM-DD (default today)")
    args = parser.parse_args(argv)
    root = Path(args.bundle)
    if not root.is_dir():
        print(f"error: not a directory: {root}", file=sys.stderr)
        return 2
    try:
        path = append_entry(root, args.message, args.actor, args.date)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"APPENDED  {path.relative_to(root).as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
