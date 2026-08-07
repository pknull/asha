#!/usr/bin/env python3
"""
workspace_status.py — enriched workspace status (v1, issue #35).

The first consumer of detect_workspace(): resolves the workspace from a
start directory (default cwd), then enriches the verdict with read-only git
state — active child repository, per-repo presence/branch/dirty state,
shared_git_root health, and manifest trackedness.

Ratified convention (Keeper, 2026-08-08; closes the proposal's open
question 1): the manifest is COMMITTED in shared_git_root. Untracked is a
WARNING; invalid is a typed ERROR with guided repair steps printed for
humans — never an auto-fix.

Exit contract (consumed by lib/workspace.sh and lib/doctor.sh):
  0 — no workspace, or a valid one (warnings do not flip the exit code)
  1 — workspace detected but manifest invalid/unreadable, or detection error
  2 — usage error
Warnings vs errors: an error means the workspace cannot be trusted at all
(fail closed); a warning is actionable but non-blocking.
"""

import contextlib
import io
import json
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import project_root  # noqa: E402


def _git_lines(args: List[str], cwd: Optional[Path] = None) -> Optional[str]:
    """Run a read-only git command; None on any failure (absent git, not a
    repo, permission). Status must never crash on a sick repository."""
    try:
        res = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    return res.stdout.strip()


def _is_repo_root(path: Path) -> bool:
    """True iff path is itself the toplevel of a git worktree.

    is-inside-work-tree is NOT enough: it answers true for any plain
    subdirectory of the surrounding workspace repo, which would report the
    parent's branch/dirty state as the child's (pass-2 blocking finding).
    """
    out = _git_lines(["-C", str(path), "rev-parse", "--show-toplevel"])
    if out is None:
        return False
    try:
        return Path(out).resolve() == path.resolve()
    except (OSError, RuntimeError, ValueError):
        return False


def _contained(candidate: Path, root: Path) -> Optional[Path]:
    """Resolved candidate iff it stays inside resolved root; else None.

    Canonical, not lexical: a declared path that is a symlink out of the
    workspace must not have git state read from the foreign target.
    """
    try:
        c = candidate.resolve()
        r = root.resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    if c == r or r in c.parents:
        return c
    return None


def _repo_state(path: Path, resolved: Optional[Path]) -> dict:
    state = {
        "exists": path.is_dir(),
        "is_git_worktree": False,
        "branch": None,
        "dirty": None,
    }
    if not state["exists"] or resolved is None:
        return state
    if not _is_repo_root(resolved):
        return state
    state["is_git_worktree"] = True
    # Unborn HEAD: abbrev-ref fails before the first commit, but the
    # symbolic branch name is still known.
    state["branch"] = (
        _git_lines(["-C", str(resolved), "rev-parse", "--abbrev-ref", "HEAD"])
        or _git_lines(["-C", str(resolved), "symbolic-ref", "--short", "HEAD"])
    )
    porcelain = _git_lines(["-C", str(resolved), "status", "--porcelain"])
    state["dirty"] = None if porcelain is None else bool(porcelain)
    return state


def _active_repository(ws_root: Path, repos: List[dict],
                       start: Path) -> Optional[str]:
    """Deepest declared repository whose tree contains start. cwd-only per
    the ratified proposal (explicit child naming is a v2 concern). Escaped
    (out-of-workspace) declarations never qualify."""
    try:
        resolved = start.resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    best: Optional[str] = None
    best_depth = -1
    for entry in repos:
        rel = entry.get("path")
        if not isinstance(rel, str):
            continue
        candidate = _contained(ws_root / rel, ws_root)
        if candidate is None:
            continue
        if candidate == resolved or candidate in resolved.parents:
            depth = len(candidate.parts)
            if depth > best_depth:
                best, best_depth = rel, depth
    return best


def build_status(start: Optional[Path] = None) -> dict:
    """Pure-read report. Never raises; every failure is typed in-report."""
    det = project_root.detect_workspace(start=start)
    report = {
        "workspace_root": str(det.root) if det.root else None,
        "workspace_name": None,
        "ok": not det.errors and (det.root is None or det.manifest is not None),
        "errors": [e._asdict() for e in det.errors],
        "warnings": [],
        "active_repository": None,
        "memory": None,
        "shared_git_root": None,
        "repositories": [],
        "manifest_tracked": None,
    }
    if det.root is None or det.manifest is None:
        return report

    manifest = det.manifest
    ws_root = det.root
    report["workspace_name"] = manifest.get("workspace_name")
    report["memory"] = manifest.get("memory")

    def warn(code: str, message: str) -> None:
        report["warnings"].append({"code": code, "message": message})

    # shared_git_root state — where workspace memory commits land.
    sgr_rel = manifest.get("memory", {}).get("shared_git_root", ".")
    sgr_resolved = (ws_root if sgr_rel == "."
                    else _contained(ws_root / sgr_rel, ws_root))
    if sgr_resolved is None:
        # Canonical escape (symlink out of the workspace): never probe git
        # state on the foreign target.
        report["shared_git_root"] = {
            "path": str(ws_root / sgr_rel),
            "is_git_worktree": False,
            "dirty": None,
        }
        warn("shared_git_root_escapes_workspace",
             f"shared_git_root ({sgr_rel}) resolves outside the workspace "
             f"root — refusing to inspect it")
        sgr_is_git = False
    else:
        sgr_is_git = _is_repo_root(sgr_resolved)
        sgr_porcelain = (
            _git_lines(["-C", str(sgr_resolved), "status", "--porcelain"])
            if sgr_is_git else None
        )
        report["shared_git_root"] = {
            "path": str(sgr_resolved),
            "is_git_worktree": sgr_is_git,
            "dirty": (bool(sgr_porcelain)
                      if sgr_porcelain is not None else None),
        }
        if not sgr_is_git:
            warn("shared_git_root_not_git",
                 f"shared_git_root ({sgr_resolved}) is not the root of a "
                 f"git worktree — workspace memory commits are unavailable "
                 f"until it is")

    # Manifest trackedness — ratified convention: committed in shared_git_root.
    if sgr_is_git and sgr_resolved is not None:
        rel_manifest = None
        try:
            rel_manifest = (ws_root / ".asha" / "workspace.json").resolve() \
                .relative_to(sgr_resolved)
        except (ValueError, OSError, RuntimeError):
            pass
        if rel_manifest is None:
            # A layout like shared_git_root="Memory" is valid, but the
            # manifest then lives OUTSIDE the commit repo — the convention
            # cannot apply, and silence would hide that (pass-2 finding).
            warn("manifest_outside_shared_git_root",
                 ".asha/workspace.json lies outside the shared_git_root "
                 "tree — the committed-manifest convention cannot apply to "
                 "this layout")
        else:
            tracked = _git_lines(
                ["-C", str(sgr_resolved), "ls-files", "--error-unmatch",
                 str(rel_manifest)]
            )
            report["manifest_tracked"] = tracked is not None
            if tracked is None:
                warn("manifest_untracked",
                     "the workspace manifest is not committed in "
                     "shared_git_root — the ratified convention is to track "
                     "it so the workspace definition is shared "
                     f"(git -C {sgr_resolved} add {rel_manifest})")

    # Declared repositories — reported, never assumed.
    start_path = Path(start if start is not None else Path.cwd())
    repos = manifest.get("repositories", [])
    for entry in repos:
        rel = entry.get("path")
        if not isinstance(rel, str):
            continue
        lexical = ws_root / rel
        resolved = _contained(lexical, ws_root)
        state = _repo_state(lexical, resolved)
        state["path"] = rel
        if "role" in entry:
            state["role"] = entry["role"]
        report["repositories"].append(state)
        if not state["exists"]:
            warn("repo_missing",
                 f"declared repository {rel} does not exist under the "
                 f"workspace root")
        elif resolved is None:
            warn("repo_escapes_workspace",
                 f"declared repository {rel} resolves outside the workspace "
                 f"root — refusing to inspect it")
        elif not state["is_git_worktree"]:
            warn("repo_not_git",
                 f"declared repository {rel} exists but is not a git "
                 f"worktree")

    report["active_repository"] = _active_repository(
        ws_root, repos, start_path
    )
    return report


def _render_human(report: dict) -> str:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        if report["workspace_root"] is None and not report["errors"]:
            print("workspace: none (single-project mode)")
        elif report["workspace_root"] is None and report["errors"]:
            # Detection itself failed (bad start dir, unreadable ancestor):
            # there is no manifest to repair — misdirecting the user to edit
            # one was a pass-2 finding.
            print("workspace: detection failed")
            for e in report["errors"]:
                print(f"  error: {e['code']} — {e['message']}")
        elif not report["ok"]:
            print(f"workspace: INVALID at {report['workspace_root'] or '?'}")
            for e in report["errors"]:
                field = f" [{e['field']}]" if e.get("field") else ""
                print(f"  error: {e['code']}{field} — {e['message']}")
            print("  repair: edit .asha/workspace.json at the workspace "
                  "root, then validate with:")
            validator = Path(__file__).resolve().parent / "workspace_manifest.py"
            print(f"    python3 {validator} <path-to-workspace.json>")
            print("  (fail-closed by design: no partial workspace behavior "
                  "until the manifest is valid)")
        else:
            print(f"workspace: {report['workspace_name']} "
                  f"at {report['workspace_root']}")
            active = report["active_repository"] or "(workspace root)"
            print(f"  active repository: {active}")
            sgr = report["shared_git_root"] or {}
            state = "git" if sgr.get("is_git_worktree") else "NOT GIT"
            dirty = ""
            if sgr.get("dirty") is not None:
                dirty = ", dirty" if sgr["dirty"] else ", clean"
            print(f"  shared_git_root: {sgr.get('path')} ({state}{dirty})")
            mem = report["memory"] or {}
            print(f"  memory: operational={mem.get('operational_root')} "
                  f"personal={mem.get('personal_root')} "
                  f"shared={mem.get('shared_root')} "
                  f"(promotion: {mem.get('promotion_mode')})")
            if report["manifest_tracked"] is not None:
                print(f"  manifest tracked: "
                      f"{'yes' if report['manifest_tracked'] else 'NO'}")
            for r in report["repositories"]:
                if not r["exists"]:
                    line = "MISSING"
                elif not r["is_git_worktree"]:
                    line = "exists, not git"
                else:
                    flags = r["branch"] or "?"
                    if r["dirty"] is not None:
                        flags += ", dirty" if r["dirty"] else ", clean"
                    line = flags
                print(f"  repo {r['path']}: {line}")
        for w in report["warnings"]:
            print(f"  warning: {w['code']} — {w['message']}")
    return out.getvalue()


def main(argv: List[str]) -> int:
    args = argv[1:]
    as_json = False
    start: Optional[Path] = None
    i = 0
    while i < len(args):
        if args[i] == "--json":
            as_json = True
        elif args[i] == "--start":
            # The value must exist and must not be another flag — silently
            # consuming "--json" as a directory was a pass-2 finding.
            if i + 1 >= len(args) or args[i + 1].startswith("--"):
                print("usage: workspace_status.py [--json] [--start DIR]",
                      file=sys.stderr)
                return 2
            start = Path(args[i + 1])
            i += 1
        elif args[i].startswith("--start="):
            value = args[i].split("=", 1)[1]
            if not value:
                print("usage: workspace_status.py [--json] [--start DIR]",
                      file=sys.stderr)
                return 2
            start = Path(value)
        else:
            print("usage: workspace_status.py [--json] [--start DIR]",
                  file=sys.stderr)
            return 2
        i += 1

    report = build_status(start=start)
    if as_json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        sys.stdout.write(_render_human(report))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
