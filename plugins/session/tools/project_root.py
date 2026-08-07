#!/usr/bin/env python3
"""
project_root.py — the ONE Python project-root resolver, plus the workspace
walk (workspace v1, delivery issue 2 — issue #33).

Before this module existed, the layered Memory-root algorithm lived in three
divergent Python copies (pattern_analyzer, event_store, learnings_manager) —
different layer sets, different walk bases, different failure modes. Each
historical caller now delegates here, declaring its exact historical
parameters, so behavior stays byte-identical while the algorithm exists
once. Do not add a new independent fallback chain anywhere — extend this.

Python detectors differ from the bash ones deliberately (and historically):
the CLAUDE_PROJECT_DIR layer is VALIDATED against Memory/ here, verbatim in
bash. That divergence is pinned, not fixed.

detect_workspace() is NEW and — in this increment — consumed by nothing:
it walks upward from a start directory for .asha/workspace.json, stopping
BEFORE $HOME and BEFORE the filesystem root (both exclusive, canonical
comparison), and validates a found manifest via workspace_manifest. An
invalid manifest is a typed verdict, never a silent keep-walking fallback —
a swallowed manifest error would be indistinguishable from "no workspace".
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import List, NamedTuple, Optional

import workspace_manifest


class WorkspaceDetection(NamedTuple):
    root: Optional[Path]
    manifest: Optional[dict]
    errors: List["workspace_manifest.ManifestError"]


_FAIL_MESSAGE = "Cannot detect project root. Ensure Memory/ directory exists."


def _argv_explicit(args: List[str]) -> Optional[Path]:
    """The historical pattern_analyzer argv scan, preserved verbatim:
    runs before argparse (import time), validates against Memory/."""
    for index, argument in enumerate(args):
        explicit = None
        if argument in {"--project-dir", "-p"} and index + 2 <= len(args):
            explicit = args[index + 1]
        elif argument.startswith("--project-dir="):
            explicit = argument.split("=", 1)[1]
        if explicit:
            project_path = Path(explicit).resolve()
            if (project_path / "Memory").is_dir():
                return project_path
    return None


def detect_project_root(
    *,
    argv: Optional[List[str]] = None,
    use_env: bool = True,
    use_git: bool = True,
    walk_base: Optional[Path] = None,
    on_fail: str = "raise",
) -> Optional[Path]:
    """Layered Memory-root resolution. Callers declare their historical set.

    argv       list to scan for --project-dir/-p (validated); None = no scan
    use_env    CLAUDE_PROJECT_DIR, validated against Memory/
    use_git    `git rev-parse --show-toplevel` accepted iff it contains Memory/
    walk_base  upward-walk start directory; None = no walk
    on_fail    "raise" (historical RuntimeError, exact message) or "none"
    """
    if argv:
        found = _argv_explicit(argv)
        if found is not None:
            return found

    if use_env:
        claude_project_dir = os.environ.get("CLAUDE_PROJECT_DIR")
        if claude_project_dir:
            project_path = Path(claude_project_dir)
            if (project_path / "Memory").is_dir():
                return project_path

    if use_git:
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                capture_output=True, text=True, check=True,
            )
            git_root = Path(result.stdout.strip())
            if (git_root / "Memory").is_dir():
                return git_root
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass

    if walk_base is not None:
        search_dir = Path(walk_base).resolve()
        while search_dir != search_dir.parent:
            if (search_dir / "Memory").is_dir():
                return search_dir
            search_dir = search_dir.parent

    if on_fail == "raise":
        raise RuntimeError(_FAIL_MESSAGE)
    return None


def detect_workspace(start: Optional[Path] = None) -> WorkspaceDetection:
    """Walk upward for .asha/workspace.json; $HOME and / are never roots.

    Returns:
      (None, None, [])       — no workspace (the overwhelmingly common case)
      (root, manifest, [])   — valid workspace
      (root, None, errors)   — manifest found but invalid/unreadable: a typed
                               fail-closed verdict. The walk STOPS at the
                               first manifest — climbing past a broken one to
                               a higher workspace would silently mask it.
    """
    candidate = Path(start if start is not None else Path.cwd()).resolve()
    home = Path(os.environ.get("HOME", str(Path.home()))).resolve()

    while True:
        # Both bounds exclusive: the user-scope config dir (~/.asha) must
        # never make $HOME a workspace, and a root-level manifest must never
        # make the whole machine one.
        if candidate == home or candidate == candidate.parent:
            return WorkspaceDetection(None, None, [])

        manifest_path = candidate / ".asha" / "workspace.json"
        if manifest_path.is_file():
            try:
                text = manifest_path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                return WorkspaceDetection(candidate, None, [
                    workspace_manifest.ManifestError(
                        "unreadable", "",
                        f"workspace manifest exists but cannot be read: {exc}",
                    )
                ])
            manifest, errors = workspace_manifest.parse_manifest_text(text)
            return WorkspaceDetection(candidate, manifest, errors)

        candidate = candidate.parent


def main(argv: List[str]) -> int:
    """CLI (consumed by later increments: status verb, save scoping).

    project_root.py workspace [--start DIR]
      Prints a JSON verdict {workspace_root, ok, errors, manifest}.
      rc 0 = no workspace or a valid one; rc 1 = manifest found but invalid.
    """
    if len(argv) < 2 or argv[1] != "workspace":
        print("usage: project_root.py workspace [--start DIR]", file=sys.stderr)
        return 2
    start: Optional[Path] = None
    rest = argv[2:]
    if rest[:1] == ["--start"] and len(rest) >= 2:
        start = Path(rest[1])
    elif rest and rest[0].startswith("--start="):
        start = Path(rest[0].split("=", 1)[1])
    elif rest:
        print("usage: project_root.py workspace [--start DIR]", file=sys.stderr)
        return 2

    det = detect_workspace(start=start)
    ok = det.root is None or det.manifest is not None
    print(json.dumps({
        "workspace_root": str(det.root) if det.root is not None else None,
        "ok": ok,
        "errors": [e._asdict() for e in det.errors],
        "manifest": det.manifest,
    }, indent=2, sort_keys=True))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
