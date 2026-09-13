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
    path = memory_v2.secure_project_root(path.absolute().parent) / path.name
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > maximum or metadata.st_uid != os.geteuid() or metadata.st_nlink != 1:
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
    expected_preimages: dict | None = None,
) -> dict:
    mapping, errors = save_scope.resolve_effective_plane("none" if explicit_none else None, start=start)
    if mapping is None:
        raise ValueError(errors[0]["message"] if errors else "save scope resolution failed")
    if mapping["scope"] != "none" or mapping["commit_repo"] is not None:
        raise ValueError("managed bare-save executor accepts only effective scope none")
    root = Path(mapping["plane_base"])
    if not expected_preimages or set(expected_preimages) != {"active", "decisions"}:
        raise ValueError("pre-draft expected digests required; re-read Memory, merge and retry")
    for value in expected_preimages.values():
        memory_v2.expected_digest(value)
    active = _draft(active_file, memory_v2.ACTIVE_LIMIT, "active draft")
    decisions = _draft(decisions_file, memory_v2.DECISIONS_LIMIT, "decisions draft")
    publication = memory_v2.publish(root, active, decisions, expected_preimages=expected_preimages)
    current = None
    verification_error = None
    try:
        current = memory_v2.snapshot_digests(memory_v2.read_published_snapshot(root))
    except (OSError, ValueError):
        verification_error = "post-publication snapshot unavailable; committed receipt retained"
    names = {"active": "Memory/activeContext.md", "decisions": "Memory/decisions.md"}
    changed = [names[key] for key in publication['changed']]
    try:
        session_id = save_identity.resolve(root, os.environ.get("ASHA_HARNESS", ""))
    except (OSError, ValueError):
        session_id = None
    return {
        "contract": "asha.managed-none-save.v1", "scope": "none",
        "plane_base": str(root), "changed": changed, "session_id": session_id,
        "identity_status": "resolved" if session_id is not None else "skipped",
        "before": {names[k]: v for k, v in publication['before'].items()},
        "after": {names[k]: v for k, v in publication['after'].items()},
        "publication": publication, "current": current,
        "superseded": current != publication['after'] if current is not None else None,
        "verification_error": verification_error,
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
    publish.add_argument("--expected-active", required=True)
    publish.add_argument("--expected-decisions", required=True)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(
            publish_managed_none(
                args.start, args.active_file, args.decisions_file,
                explicit_none=args.scope == "none",
                expected_preimages={"active": args.expected_active, "decisions": args.expected_decisions},
            ),
            sort_keys=True,
        ))
        return 0
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
