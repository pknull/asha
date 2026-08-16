#!/usr/bin/env python3
"""Execute the managed bare-save publication path without any Git seam."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
from pathlib import Path

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import memory_v2
import save_identity
import save_scope


def _draft(path: Path, maximum: int, label: str) -> str:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > maximum:
            raise ValueError(f"{label} is not one bounded regular file")
        chunks: list[bytes] = []
        remaining = maximum + 1
        while remaining:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > maximum:
            raise ValueError(f"{label} exceeds {maximum} UTF-8 bytes")
        return raw.decode("utf-8")
    finally:
        os.close(fd)


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def publish_managed_none(
    start: Path, active_file: Path, decisions_file: Path, *, explicit_none: bool = False,
) -> dict:
    mapping, errors = save_scope.resolve_effective_plane("none" if explicit_none else None, start=start)
    if mapping is None:
        raise ValueError(errors[0]["message"] if errors else "save scope resolution failed")
    if mapping["scope"] != "none" or mapping["commit_repo"] is not None:
        raise ValueError("managed bare-save executor accepts only effective scope none")
    root = Path(mapping["plane_base"])
    before = memory_v2.read_published_snapshot(root)
    active = _draft(active_file, memory_v2.ACTIVE_LIMIT, "active draft")
    decisions = _draft(decisions_file, memory_v2.DECISIONS_LIMIT, "decisions draft")
    memory_v2.publish(root, active, decisions)
    after = memory_v2.read_published_snapshot(root)
    if after.active_context != active.encode("utf-8") or after.decisions != decisions.encode("utf-8"):
        raise ValueError("published Memory bytes differ from the validated drafts")
    changed = []
    for relative, old, new in (
        ("Memory/activeContext.md", before.active_context, after.active_context),
        ("Memory/decisions.md", before.decisions, after.decisions),
    ):
        if old != new:
            changed.append(relative)
    try:
        session_id = save_identity.resolve(root, os.environ.get("ASHA_HARNESS", ""))
    except (OSError, ValueError):
        session_id = None
    return {
        "contract": "asha.managed-none-save.v1", "scope": "none",
        "plane_base": str(root), "changed": changed, "session_id": session_id,
        "identity_status": "resolved" if session_id is not None else "skipped",
        "before": {
            "Memory/activeContext.md": _digest(before.active_context),
            "Memory/decisions.md": _digest(before.decisions),
        },
        "after": {
            "Memory/activeContext.md": _digest(after.active_context),
            "Memory/decisions.md": _digest(after.decisions),
        },
        "git_invoked": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Managed no-Git Memory publication")
    sub = parser.add_subparsers(dest="command", required=True)
    publish = sub.add_parser("publish")
    publish.add_argument("--start", required=True, type=Path)
    publish.add_argument("--scope", choices=("none",))
    publish.add_argument("--active-file", required=True, type=Path)
    publish.add_argument("--decisions-file", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(
            publish_managed_none(
                args.start, args.active_file, args.decisions_file,
                explicit_none=args.scope == "none",
            ),
            sort_keys=True,
        ))
        return 0
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
