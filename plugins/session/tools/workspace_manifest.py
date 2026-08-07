#!/usr/bin/env python3
"""
workspace_manifest.py — pure lexical parse/validate of .asha/workspace.json.

Workspace v1, delivery issue 1 (issue #31) of the ratified proposal
docs/proposals/2026-08-06--workspace-memory.md. This layer is deliberately
filesystem-free: git-worktree existence, symlink canonicalization, and real
root resolution belong to detection/status (issues 2-3), where an actual
workspace root exists. Everything here is decidable from the manifest text
alone, so it stays a pure, TOTAL function with table-driven tests — hostile
input (cycles, absurd depth, huge integers, NUL/surrogate paths) yields a
typed fail-closed error, never an exception and never a bogus success.

Contract (pinned by the proposal):
  - Typed, COLLECTED errors (stable code + field + message) — not fail-fast.
  - Fail closed: any error means no manifest object is returned.
  - Schema defaults applied; unknown keys preserved at every level, never
    stripped (forward compatibility).
  - Path rules are lexical: workspace-relative only; absolute paths and any
    `..` segment reject; `.` rejects everywhere except shared_git_root.
  - Containment: operational_root must sit inside the shared_git_root tree.
  - Disjointness: the three memory roots pairwise non-nesting.
  - v1 value pin: operational_root must equal "Memory".

Validator strictness beyond the proposal's text (fail-closed choices,
surfaced for Keeper ratification on PR #32 rather than invented silently):
  - backslashes, Windows drive/UNC forms, control characters (incl. NUL,
    newline, tab), and lone surrogates reject — a path the runtime cannot
    even stat, or that only a Windows resolver could interpret, must fail
    inside this typed-error boundary, not crash canonicalization later;
  - a whitespace-only workspace_name rejects;
  - duplicate repository paths (after normalization) reject.
Accepted residuals: `~` and `$VAR` are treated as literal names (downstream
consumers must never shell-interpolate manifest paths unquoted), and path
length is unbounded here.
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

_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")


def _normalize_relpath(value: Any) -> Tuple[Optional[str], Optional[str]]:
    """Lexically normalize a workspace-relative path.

    Returns (normalized, None) or (None, error_code). "." and "./" normalize
    to "." — callers decide whether "." is legal for their field.
    """
    if not isinstance(value, str):
        return None, "wrong_type"
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        # Lone surrogates cannot reach os.stat or git at all.
        return None, "invalid_path"
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in value):
        # NUL crashes every fs call; newline/tab/controls only ever appear in
        # a manifest as an accident or an injection attempt. Fail here, typed.
        return None, "invalid_path"
    if value.strip() == "":
        return None, "invalid_path"
    if _WINDOWS_DRIVE.match(value):
        # C:/x, C:\x, and drive-relative C:x all resolve against a drive,
        # never against the workspace root.
        return None, "absolute_path"
    if value.startswith("\\\\"):
        return None, "absolute_path"
    if value.startswith("/"):
        return None, "absolute_path"
    if "\\" in value:
        # POSIX-only manifests: a backslash is ambiguous (separator on
        # Windows, literal elsewhere) — refuse rather than guess.
        return None, "invalid_path"
    parts = [p for p in value.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        # ANY dot-dot segment rejects, even lexically-contained "a/../b":
        # normalization must never launder traversal.
        return None, "path_traversal"
    if not parts:
        return ".", None
    return "/".join(parts), None


def _nests(a: str, b: str) -> bool:
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def _validate_version(data: dict, errors: List[ManifestError]) -> None:
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


def _validate_workspace_name(data: dict, errors: List[ManifestError]) -> None:
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


def _validate_memory_roots(
    mem_out: dict, errors: List[ManifestError]
) -> dict:
    """Normalize the four root paths in place; return the normalized subset."""
    normalized: dict = {}
    for key in ("operational_root", "personal_root", "shared_root",
                "shared_git_root"):
        field = f"memory.{key}"
        norm, err = _normalize_relpath(mem_out[key])
        if err:
            errors.append(
                ManifestError(
                    err, field, f"{key} is not a valid workspace-relative path"
                )
            )
            continue
        if norm == "." and key != "shared_git_root":
            # Only the commit repo may be the workspace root itself; a plane
            # root of "." would contain every other root.
            errors.append(
                ManifestError(
                    "invalid_path", field,
                    f"{key} may not be the workspace root itself",
                )
            )
            continue
        mem_out[key] = norm
        normalized[key] = norm
    return normalized


def _check_root_relations(
    roots: dict, errors: List[ManifestError]
) -> None:
    """v1 pin, pairwise disjointness, and containment over normalized roots."""
    op = roots.get("operational_root")
    if op is not None and op != V1_OPERATIONAL_ROOT:
        errors.append(
            ManifestError(
                "operational_root_reserved",
                "memory.operational_root",
                f"operational_root must be \"{V1_OPERATIONAL_ROOT}\" in v1 — "
                f"the save preflight, commit gate, and event machinery key on "
                f"that literal path; other values are reserved for a future "
                f"increment",
            )
        )

    seen = [(k, roots[k]) for k in _PLANE_ROOTS if k in roots]
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

    sgr = roots.get("shared_git_root")
    if op is not None and sgr is not None:
        if not (sgr == "." or op == sgr or op.startswith(sgr + "/")):
            # The relation involves both fields — blame "memory", not the
            # (pinned-correct) operational_root, so repair guidance points
            # at the pair rather than the wrong knob.
            errors.append(
                ManifestError(
                    "containment_violation",
                    "memory",
                    f"operational_root ({op}) resolves outside the "
                    f"shared_git_root worktree ({sgr}) — the write root "
                    f"and the commit repo cannot diverge",
                )
            )


def _validate_memory(raw_memory: Any, errors: List[ManifestError]) -> Any:
    if not isinstance(raw_memory, dict):
        errors.append(
            ManifestError("wrong_type", "memory", "memory must be an object")
        )
        return raw_memory
    mem_out = dict(copy.deepcopy(raw_memory))
    for key, default in MEMORY_DEFAULTS.items():
        mem_out.setdefault(key, default)

    normalized = _validate_memory_roots(mem_out, errors)
    _check_root_relations(normalized, errors)

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
    return mem_out


def _validate_repo_entry(
    entry: Any, i: int, seen_paths: dict, errors: List[ManifestError]
) -> Any:
    field = f"repositories[{i}]"
    if not isinstance(entry, dict):
        errors.append(
            ManifestError("wrong_type", field, "repository entry must be an object")
        )
        return entry

    # Every field is validated even when another is missing or broken —
    # the collected-errors contract means one verdict repairs the whole entry
    # (pass-2 finding: an early `continue` here suppressed role/docs errors).
    if "path" not in entry:
        errors.append(
            ManifestError(
                "missing_field", f"{field}.path", "repository entry requires a path"
            )
        )
    else:
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
                    "a child repository must be a proper subdirectory of the "
                    "workspace, not the workspace root itself",
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
            ManifestError("wrong_type", f"{field}.role", "role must be a string")
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
    return entry


def _validate_repositories(raw_repos: Any, errors: List[ManifestError]) -> Any:
    if not isinstance(raw_repos, list):
        errors.append(
            ManifestError("wrong_type", "repositories", "repositories must be a list")
        )
        return raw_repos
    repos_out = []
    seen_paths: dict = {}
    for i, entry in enumerate(copy.deepcopy(raw_repos)):
        repos_out.append(_validate_repo_entry(entry, i, seen_paths, errors))
    return repos_out


def _representable(data: dict) -> bool:
    """Probe that the structure is finite, acyclic, JSON-typed, and printable.

    json.dumps raises ValueError on cycles and over-limit integers, TypeError
    on non-JSON values, RecursionError on absurd depth — exactly the inputs
    that would otherwise crash validation or produce an unserializable
    "valid" manifest.
    """
    try:
        json.dumps(data)
        return True
    except (ValueError, TypeError, RecursionError, OverflowError):
        return False


_UNPROCESSABLE = ManifestError(
    "unprocessable",
    "",
    "manifest structure is not JSON-representable (cycle, non-JSON value, "
    "over-limit number, or excessive depth)",
)


def _validate(data: Any) -> Tuple[Optional[dict], List[ManifestError]]:
    if not isinstance(data, dict):
        return None, [
            ManifestError("not_object", "", "manifest root must be a JSON object")
        ]
    if not _representable(data):
        return None, [_UNPROCESSABLE]

    errors: List[ManifestError] = []
    out = copy.deepcopy(data)

    _validate_version(data, errors)
    _validate_workspace_name(data, errors)
    out["memory"] = _validate_memory(data.get("memory", {}), errors)
    out["repositories"] = _validate_repositories(
        data.get("repositories", []), errors
    )

    if errors:
        return None, errors
    return out, []


def validate_manifest(data: Any) -> Tuple[Optional[dict], List[ManifestError]]:
    """Validate a parsed manifest. Pure, total; never touches the filesystem.

    Returns (manifest, []) on success — a deep copy with defaults applied and
    paths normalized, unknown keys intact — or (None, errors) on any failure.
    """
    try:
        return _validate(data)
    except RecursionError:
        # Belt over the _representable braces: dumps and deepcopy hit their
        # recursion ceilings at slightly different depths.
        return None, [_UNPROCESSABLE]


def _reject_json_constant(name: str) -> None:
    # NaN/Infinity are Python extensions, not JSON; strict consumers of the
    # manifest (jq, other runtimes) would reject what we accepted.
    raise ValueError(f"non-standard JSON constant: {name}")


def parse_manifest_text(text: str) -> Tuple[Optional[dict], List[ManifestError]]:
    """Parse manifest JSON text and validate it. Fail closed on bad JSON."""
    try:
        data = json.loads(text, parse_constant=_reject_json_constant)
    except (ValueError, TypeError, RecursionError) as exc:
        # JSONDecodeError, the int-digit limit, NaN/Infinity rejection, and
        # parser recursion all land here — one typed verdict, no crash.
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
        with open(argv[1], "r", encoding="utf-8") as fh:
            text = fh.read()
    except (OSError, UnicodeDecodeError) as exc:
        print(json.dumps({"ok": False, "manifest": None, "errors": [
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
