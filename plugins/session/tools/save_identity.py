#!/usr/bin/env python3
"""Resolve explicit-save evidence identity from available native seams.

The result is a local corroboration heuristic.  It is not a security token:
session environments and recovery files are user-controlled like the rest of
Asha's local Memory state.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import recovery_state
from path_safety import secure_project_root


ENV_SEAMS = ("ASHA_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID")


def _valid(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or value == "unknown" or "[REDACTED" in value:
        return None
    return value


def resolve(project_dir: Path, harness: str = "") -> str:
    root = secure_project_root(project_dir, reject_home=True)
    for name in ENV_SEAMS:
        value = _valid(os.environ.get(name))
        if value:
            return value
    if harness.strip().lower() == "copilot":
        recovery_state.sweep(root)
        snapshot = recovery_state.latest(root)
        if snapshot and str(snapshot.get("harness", "")).lower() == "copilot":
            value = _valid(snapshot.get("session_id"))
            if value:
                return value
    raise ValueError("native explicit-save session identity unavailable")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Resolve explicit-save session identity")
    parser.add_argument("--project-dir", required=True, type=Path)
    parser.add_argument("--harness", default=os.environ.get("ASHA_HARNESS", ""))
    args = parser.parse_args(argv)
    try:
        print(resolve(args.project_dir, args.harness))
        return 0
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
