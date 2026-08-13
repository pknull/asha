#!/usr/bin/env python3
"""Resolve explicit Memory v2 publication scopes.

A plane mapping keeps THREE distinct values (conflating them is how the
cross-plane bypass happens):
  plane_base   — where this plane's Memory/ and Work/ live
  memory_root  — the ONLY pathspec a save for this scope may stage
  commit_repo  — the git worktree the commit lands in

Scopes (per the ratified proposal):
  repo       — the active child project (or the sole project outside any
               workspace): plane_base = commit_repo = the project root.
               From the workspace root itself this FAILS with guidance.
  workspace  — plane_base = workspace root, commit_repo = shared_git_root,
               memory_root = the workspace operational plane. Requires a
               valid manifest and a real git worktree at shared_git_root —
               fail closed otherwise.

This module resolves paths only. Memory validation and atomic publication live
in memory_v2.py; the explicit save command owns Git staging and publication.
"""

import json
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import project_root  # noqa: E402

SCHEMA_VERSION = 2


def _err(code: str, message: str) -> dict:
    return {"code": code, "field": "", "message": message}


def _is_repo_root(path: Path) -> bool:
    try:
        res = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if res.returncode != 0:
        return False
    try:
        return Path(res.stdout.strip()).resolve() == path.resolve()
    except (OSError, RuntimeError, ValueError):
        return False


def resolve_plane(scope: str, start: Optional[Path] = None
                  ) -> Tuple[Optional[dict], List[dict]]:
    """Resolve a scope into its plane mapping. Fail closed, typed errors."""
    if scope not in ("repo", "workspace"):
        return None, [_err("invalid_scope",
                           f"scope must be repo|workspace, got {scope!r}")]
    start_path = Path(start if start is not None else Path.cwd())
    det = project_root.detect_workspace(start=start_path)
    if det.errors:
        return None, [e._asdict() for e in det.errors]

    if scope == "workspace":
        if det.root is None:
            return None, [_err("no_workspace",
                               "no .asha/workspace.json in any ancestor — "
                               "--scope workspace requires a workspace")]
        manifest = det.manifest
        assert manifest is not None  # det.errors empty + root set => valid
        ws_root = det.root
        mem = manifest.get("memory", {})
        sgr_rel = mem.get("shared_git_root", ".")
        sgr = ws_root if sgr_rel == "." else (ws_root / sgr_rel).resolve()
        if not _is_repo_root(sgr):
            return None, [_err(
                "shared_git_root_not_git",
                f"shared_git_root ({sgr}) is not the root of a git worktree "
                f"— workspace memory commits are impossible; failing closed",
            )]
        # Canonical runtime containment (defense-in-depth over the lexical
        # validator): memory_root must land inside commit_repo AFTER symlink
        # resolution, and memory_rel is computed here so consumers never
        # re-derive it with string stripping (pass-2: an absolute REL from a
        # failed strip silently never matched staged paths).
        try:
            mem_root = (ws_root / mem.get("operational_root",
                                          "Memory")).resolve()
            sgr_resolved = sgr.resolve()
            memory_rel = str(mem_root.relative_to(sgr_resolved))
        except (ValueError, OSError, RuntimeError) as exc:
            return None, [_err(
                "containment_violation",
                f"operational_root does not resolve inside shared_git_root "
                f"({exc}) — failing closed",
            )]
        if memory_rel == ".":
            memory_rel = ""
        return {
            "version": SCHEMA_VERSION,
            "scope": "workspace",
            "plane_base": str(ws_root),
            "memory_root": str(mem_root),
            "memory_rel": memory_rel,
            "commit_repo": str(sgr_resolved),
        }, []

    # scope == "repo": the active child project. Outside a workspace this is
    # simply the surrounding project; inside one, cwd must be in a child.
    if det.root is not None:
        manifest = det.manifest
        assert manifest is not None
        active = None
        try:
            resolved = start_path.resolve()
        except (OSError, RuntimeError, ValueError):
            resolved = None
        if resolved is not None:
            best_depth = -1
            for entry in manifest.get("repositories", []):
                rel = entry.get("path")
                if not isinstance(rel, str):
                    continue
                try:
                    cand = (det.root / rel).resolve()
                except (OSError, RuntimeError, ValueError):
                    continue
                if cand == resolved or cand in resolved.parents:
                    if len(cand.parts) > best_depth:
                        active, best_depth = cand, len(cand.parts)
        if active is None:
            return None, [_err(
                "no_active_repo",
                "--scope repo needs the current directory inside a declared "
                "child repository; from the workspace root use "
                "--scope workspace, or cd into a child",
            )]
        base = active
    else:
        top = None
        try:
            res = subprocess.run(
                ["git", "-C", str(start_path), "rev-parse",
                 "--show-toplevel"],
                capture_output=True, text=True, timeout=30,
            )
            if res.returncode == 0:
                top = Path(res.stdout.strip())
        except (OSError, subprocess.SubprocessError):
            pass
        if top is None:
            return None, [_err("no_repo",
                               "not inside a git repository and no "
                               "workspace manifest found")]
        base = top

    return {
        "version": SCHEMA_VERSION,
        "scope": "repo",
        "plane_base": str(base),
        "memory_root": str(base / "Memory"),
        "memory_rel": "Memory",
        "commit_repo": str(base),
    }, []


def _resolve_from_args(scope: str, start: Optional[str]
                       ) -> Tuple[Optional[dict], List[dict]]:
    return resolve_plane(scope, Path(start) if start else None)


def main(argv: List[str]) -> int:
    """CLI: resolve --scope S [--start D]."""
    args = argv[1:]
    if not args or args[0] != "resolve":
        print("usage: save_scope.py resolve "
              "--scope repo|workspace [--start DIR]", file=sys.stderr)
        return 2
    verb, rest = args[0], args[1:]
    scope, start = None, None
    i = 0
    while i < len(rest):
        if rest[i] == "--scope" and i + 1 < len(rest):
            scope = rest[i + 1]; i += 1
        elif rest[i] == "--start" and i + 1 < len(rest):
            start = rest[i + 1]; i += 1
        else:
            print("usage: save_scope.py resolve "
                  "--scope repo|workspace [--start DIR]", file=sys.stderr)
            return 2
        i += 1
    if not scope:
        print("usage: --scope is required", file=sys.stderr)
        return 2

    mapping, errors = _resolve_from_args(scope, start)
    if mapping is None:
        print(json.dumps({"errors": errors}, indent=2))
        return 1
    print(json.dumps(mapping, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
