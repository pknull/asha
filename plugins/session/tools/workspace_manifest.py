#!/usr/bin/env python3
"""
workspace_manifest.py — pure lexical parse/validate of .asha/workspace.json.

Workspace v1, delivery issue 1 (issue #31) of the ratified proposal
docs/proposals/2026-08-06--workspace-memory.md. This layer is deliberately
filesystem-free: git-worktree existence, symlink canonicalization, and real
root resolution belong to detection/status (issues 2-3), where an actual
workspace root exists. Everything here is decidable from the manifest text
alone, so it stays a pure function with table-driven tests.

Contract (pinned by the proposal):
  - Typed, COLLECTED errors (stable code + field + message) — not fail-fast.
  - Fail closed: any error means no manifest object is returned.
  - Schema defaults applied; unknown keys preserved at every level, never
    stripped (forward compatibility).
  - Path rules are lexical: workspace-relative only; absolute paths,
    backslashes, and any `..` segment reject; `.` rejects everywhere except
    shared_git_root.
  - Containment: operational_root must sit inside the shared_git_root tree
    (the write root and the commit repo cannot diverge).
  - Disjointness: the three memory roots pairwise non-nesting.
  - v1 value pin: operational_root must equal "Memory" — the save preflight,
    hash-bound commit gate, and event machinery key on that literal path.
"""

import copy
import json
import re
import sys
from typing import Any, List, NamedTuple, Optional, Tuple


class ManifestError(NamedTuple):
    code: str
    field: str
    message: str


MEMORY_DEFAULTS = {
    "operational_root": "Memory",
    "personal_root": "memory-local",
    "shared_root": "knowledge",
    "shared_git_root": ".",
    "promotion_mode": "pull-request",
}
PROMOTION_MODES = ("pull-request", "direct-commit")
V1_OPERATIONAL_ROOT = "Memory"
SUPPORTED_VERSION = 1

# The three plane roots subject to pairwise disjointness. shared_git_root is
# the commit repo, not a plane root — it participates in containment instead.
_PLANE_ROOTS = ("operational_root", "personal_root", "shared_root")

_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:($|/)")


def _normalize_relpath(value: Any) -> Tuple[Optional[str], Optional[str]]:
    """Lexically normalize a workspace-relative path.

    Returns (normalized, None) or (None, error_code). "." and "./" normalize
    to "." — callers decide whether "." is legal for their field.
    """
    if not isinstance(value, str):
        return None, "wrong_type"
    if value.strip() == "":
        return None, "invalid_path"
    if "\\" in value:
        # POSIX-only manifests: a backslash is ambiguous (separator on
        # Windows, literal elsewhere) — refuse rather than guess.
        return None, "invalid_path"
    if value.startswith("/"):
        return None, "absolute_path"
    if _WINDOWS_DRIVE.match(value):
        return None, "absolute_path"
    parts = [p for p in value.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        # Interior dot-dot segments must not be laundered by normalization:
        # "a/../../b" escapes the root even though it contains no leading "..".
        return None, "path_traversal"
    if not parts:
        return ".", None
    return "/".join(parts), None


def _nests(a: str, b: str) -> bool:
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def validate_manifest(data: Any) -> Tuple[Optional[dict], List[ManifestError]]:
    """Validate a parsed manifest dict. Pure; never touches the filesystem.

    Returns (manifest, []) on success — a deep copy with defaults applied and
    paths normalized, unknown keys intact — or (None, errors) on any failure.
    """
    errors: List[ManifestError] = []

    if not isinstance(data, dict):
        return None, [
            ManifestError("not_object", "", "manifest root must be a JSON object")
        ]

    out = copy.deepcopy(data)

    # -- version -------------------------------------------------------------
    if "version" not in data:
        errors.append(
            ManifestError("missing_field", "version", "required field is absent")
        )
    elif isinstance(data["version"], bool) or not isinstance(data["version"], int):
        # bool is an int subclass in Python; True must not read as version 1.
        errors.append(
            ManifestError("wrong_type", "version", "version must be an integer")
        )
    elif data["version"] != SUPPORTED_VERSION:
        errors.append(
            ManifestError(
                "unsupported_version",
                "version",
                f"version {data['version']} is not supported (this build "
                f"understands {SUPPORTED_VERSION}); failing closed rather "
                f"than guessing at a newer contract",
            )
        )

    # -- workspace_name --------------------------------------------------------
    if "workspace_name" not in data:
        errors.append(
            ManifestError(
                "missing_field", "workspace_name", "required field is absent"
            )
        )
    elif not isinstance(data["workspace_name"], str):
        errors.append(
            ManifestError(
                "wrong_type", "workspace_name", "workspace_name must be a string"
            )
        )
    elif data["workspace_name"].strip() == "":
        errors.append(
            ManifestError(
                "empty_value", "workspace_name", "workspace_name must be non-empty"
            )
        )

    # -- memory roots ----------------------------------------------------------
    raw_memory = data.get("memory", {})
    normalized_roots: dict = {}
    if not isinstance(raw_memory, dict):
        errors.append(
            ManifestError("wrong_type", "memory", "memory must be an object")
        )
    else:
        mem_out = dict(copy.deepcopy(raw_memory))
        for key, default in MEMORY_DEFAULTS.items():
            mem_out.setdefault(key, default)

        for key in ("operational_root", "personal_root", "shared_root",
                    "shared_git_root"):
            field = f"memory.{key}"
            norm, err = _normalize_relpath(mem_out[key])
            if err:
                errors.append(
                    ManifestError(err, field, f"{key} is not a valid "
                                              f"workspace-relative path")
                )
                continue
            if norm == "." and key != "shared_git_root":
                # Only the commit repo may be the workspace root itself; a
                # plane root of "." would contain every other root.
                errors.append(
                    ManifestError(
                        "invalid_path", field,
                        f"{key} may not be the workspace root itself",
                    )
                )
                continue
            mem_out[key] = norm
            normalized_roots[key] = norm

        op = normalized_roots.get("operational_root")
        if op is not None and op != V1_OPERATIONAL_ROOT:
            errors.append(
                ManifestError(
                    "operational_root_reserved",
                    "memory.operational_root",
                    f"operational_root must be \"{V1_OPERATIONAL_ROOT}\" in v1 "
                    f"— the save preflight, commit gate, and event machinery "
                    f"key on that literal path; other values are reserved for "
                    f"a future increment",
                )
            )

        mode = mem_out["promotion_mode"]
        if not isinstance(mode, str):
            errors.append(
                ManifestError(
                    "wrong_type", "memory.promotion_mode",
                    "promotion_mode must be a string",
                )
            )
        elif mode not in PROMOTION_MODES:
            errors.append(
                ManifestError(
                    "invalid_promotion_mode",
                    "memory.promotion_mode",
                    f"promotion_mode must be one of {list(PROMOTION_MODES)}",
                )
            )

        # Disjointness: none of the three plane roots may nest inside another.
        seen = [(k, normalized_roots[k]) for k in _PLANE_ROOTS
                if k in normalized_roots]
        for i in range(len(seen)):
            for j in range(i + 1, len(seen)):
                (ka, va), (kb, vb) = seen[i], seen[j]
                if _nests(va, vb):
                    errors.append(
                        ManifestError(
                            "roots_not_disjoint",
                            "memory",
                            f"{ka} ({va}) and {kb} ({vb}) overlap — a save "
                            f"staging one root must never pick up the other",
                        )
                    )

        # Containment: the write root must live inside the commit repo.
        sgr = normalized_roots.get("shared_git_root")
        if op is not None and sgr is not None:
            inside = sgr == "." or op == sgr or op.startswith(sgr + "/")
            if not inside:
                errors.append(
                    ManifestError(
                        "containment_violation",
                        "memory.operational_root",
                        f"operational_root ({op}) resolves outside the "
                        f"shared_git_root worktree ({sgr}) — the write root "
                        f"and the commit repo cannot diverge",
                    )
                )

        out["memory"] = mem_out

    # -- repositories ------------------------------------------------------------
    raw_repos = data.get("repositories", [])
    if not isinstance(raw_repos, list):
        errors.append(
            ManifestError(
                "wrong_type", "repositories", "repositories must be a list"
            )
        )
    else:
        repos_out = []
        seen_paths: dict = {}
        for i, entry in enumerate(copy.deepcopy(raw_repos)):
            field = f"repositories[{i}]"
            if not isinstance(entry, dict):
                errors.append(
                    ManifestError(
                        "wrong_type", field, "repository entry must be an object"
                    )
                )
                continue
            if "path" not in entry:
                errors.append(
                    ManifestError(
                        "missing_field", f"{field}.path",
                        "repository entry requires a path",
                    )
                )
                repos_out.append(entry)
                continue
            norm, err = _normalize_relpath(entry["path"])
            if err:
                errors.append(
                    ManifestError(
                        err, f"{field}.path",
                        "repository path is not a valid workspace-relative path",
                    )
                )
            elif norm == ".":
                errors.append(
                    ManifestError(
                        "repo_path_not_child", f"{field}.path",
                        "a child repository must be a proper subdirectory of "
                        "the workspace, not the workspace root itself",
                    )
                )
            else:
                if norm in seen_paths:
                    errors.append(
                        ManifestError(
                            "duplicate_repository", f"{field}.path",
                            f"repository path {norm} already declared at "
                            f"repositories[{seen_paths[norm]}]",
                        )
                    )
                else:
                    seen_paths[norm] = i
                entry["path"] = norm

            if "role" in entry and not isinstance(entry["role"], str):
                errors.append(
                    ManifestError(
                        "wrong_type", f"{field}.role", "role must be a string"
                    )
                )
            if "docs" in entry:
                dnorm, derr = _normalize_relpath(entry["docs"])
                if derr:
                    errors.append(
                        ManifestError(
                            derr, f"{field}.docs",
                            "docs is not a valid workspace-relative path",
                        )
                    )
                elif dnorm == ".":
                    errors.append(
                        ManifestError(
                            "invalid_path", f"{field}.docs",
                            "docs may not be the workspace root itself",
                        )
                    )
                else:
                    entry["docs"] = dnorm
            repos_out.append(entry)
        out["repositories"] = repos_out

    if errors:
        return None, errors
    return out, []


def parse_manifest_text(text: str) -> Tuple[Optional[dict], List[ManifestError]]:
    """Parse manifest JSON text and validate it. Fail closed on bad JSON."""
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError) as exc:
        return None, [
            ManifestError("invalid_json", "", f"manifest is not valid JSON: {exc}")
        ]
    return validate_manifest(data)


def main(argv: List[str]) -> int:
    """CLI: validate a manifest file, print a JSON verdict, exit 0/1.

    Consumed by later increments (detection, status) and handy for humans:
    `python3 workspace_manifest.py <path>`.
    """
    if len(argv) != 2:
        print("usage: workspace_manifest.py <path-to-workspace.json>",
              file=sys.stderr)
        return 2
    try:
        text = open(argv[1], "r", encoding="utf-8").read()
    except OSError as exc:
        print(json.dumps({"ok": False, "errors": [
            {"code": "unreadable", "field": "", "message": str(exc)}]}))
        return 1
    manifest, errors = parse_manifest_text(text)
    verdict = {
        "ok": manifest is not None,
        "errors": [e._asdict() for e in errors],
        "manifest": manifest,
    }
    print(json.dumps(verdict, indent=2, sort_keys=True))
    return 0 if manifest is not None else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
