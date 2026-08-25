"""Project index for the coordinator: which repositories an intent may target.

A declared workspace manifest (`.asha/workspace.json` at or above the start
directory) is the index when present. Otherwise the index is a bounded,
read-only discovery of jj-colocated Asha projects (Memory v2 project plus a
colocated jj/git pair) at the start directory and its immediate children.
Nothing here writes, touches jj, or infers identity beyond what the project's
own published configuration states.
"""

from __future__ import annotations

import json
import os
from collections import deque
from pathlib import Path
from typing import Any, Mapping

from ..context import detect_workspace, read_published_snapshot, validate_manifest

PROJECT_LIST_CONTRACT = "asha.orchestration-project-list.v1"
MAX_DISCOVERED_DIRECTORIES = 512
MAX_DEPTH = 3


class ProjectIndexError(ValueError):
    """The project index could not be built deterministically."""


def _is_asha_project(path: Path) -> bool:
    try:
        return (path / ".asha" / "config.json").is_file() and (path / "Memory").is_dir()
    except OSError:
        return False


def _project_config(root: Path) -> dict[str, Any]:
    try:
        value = json.loads((root / ".asha" / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def display_name(root: Path, fallback: str) -> str:
    """The project's own `name`, or the directory name.

    A project states its own friendly name so an operator picks from words they
    chose rather than from directory basenames or truncated UUIDs. Bounded and
    printable-checked because it is rendered straight into a terminal row.
    """
    value = _project_config(root).get("name")
    if not isinstance(value, str):
        return fallback
    cleaned = " ".join(value.split())
    if not cleaned or len(cleaned) > 48 or not cleaned.isprintable():
        return fallback
    return cleaned


def _project_id(root: Path) -> str | None:
    try:
        return read_published_snapshot(root).project_id
    except Exception:  # noqa: BLE001 - unpublished project: fall back to its own config
        pass
    project_id = _project_config(root).get("project_id")
    return project_id if isinstance(project_id, str) and project_id else None


def _entry(root: Path, *, name: str, role: str | None, declared: bool) -> dict[str, Any]:
    return {
        "name": display_name(root, name),
        "directory": name,
        "root": str(root),
        "project_id": _project_id(root),
        "role": role,
        "declared": declared,
        "asha_project": _is_asha_project(root),
        "jj_colocated": (root / ".jj").is_dir() and (root / ".git").exists(),
    }


def _declared_entries(workspace_root: Path, manifest: Mapping[str, Any]) -> list[dict[str, Any]]:
    entries = []
    for item in manifest.get("repositories", []):
        root = (workspace_root / item["path"]).resolve()
        role = item.get("role")
        entries.append(_entry(root, name=Path(item["path"]).name, role=role if isinstance(role, str) else None, declared=True))
    return entries


def _discovered_entries(start: Path, depth: int) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if _is_asha_project(start):
        found.append(_entry(start, name=start.name, role=None, declared=False))
    queue: deque[tuple[Path, int]] = deque([(start, 0)])
    scanned = 0
    while queue:
        directory, level = queue.popleft()
        if level >= depth:
            continue
        try:
            children = sorted(
                child for child in directory.iterdir()
                if child.is_dir() and not child.is_symlink() and not child.name.startswith(".")
            )
        except OSError:
            continue
        for child in children:
            scanned += 1
            if scanned > MAX_DISCOVERED_DIRECTORIES:
                raise ProjectIndexError(
                    f"discovery stopped after {MAX_DISCOVERED_DIRECTORIES} directories; use --root or a workspace manifest"
                )
            if _is_asha_project(child):
                found.append(_entry(child, name=child.name, role=None, declared=False))
            else:
                queue.append((child, level + 1))
    return sorted(found, key=lambda item: (item["name"], item["root"]))


def _matches(entry: Mapping[str, Any], text: str) -> bool:
    needle = text.strip().lower()
    if not needle:
        return True
    return needle in {
        entry["name"].lower(), str(entry.get("directory") or "").lower(),
        Path(entry["root"]).name.lower(), str(entry["project_id"] or "").lower(),
    }


def list_projects(start: Path, *, depth: int = 1, match: str | None = None) -> dict[str, Any]:
    """Build the closed project-list payload for `start`."""
    if not 1 <= depth <= MAX_DEPTH:
        raise ProjectIndexError(f"depth must be between 1 and {MAX_DEPTH}")
    root = Path(start).expanduser().resolve()
    if not root.is_dir():
        raise ProjectIndexError(f"project root is not a directory: {start}")
    detection = detect_workspace(root)
    if detection.errors:
        detail = "; ".join(f"{error.code}: {error.message}" for error in detection.errors)
        raise ProjectIndexError(f"workspace detection failed: {detail}")
    if detection.root is not None and detection.manifest is not None:
        manifest, errors = validate_manifest(detection.manifest)
        if manifest is None:
            detail = "; ".join(f"{error.code}: {error.message}" for error in errors)
            raise ProjectIndexError(f"workspace manifest is invalid: {detail}")
        source, index_root = "manifest", detection.root.resolve()
        entries = _declared_entries(index_root, manifest)
    else:
        source, index_root = "discovery", root
        entries = _discovered_entries(root, depth)
    if match is not None:
        entries = [entry for entry in entries if _matches(entry, match)]
    return {
        "contract": PROJECT_LIST_CONTRACT,
        "root": str(index_root),
        "source": source,
        "match": match,
        "projects": entries,
    }


USER_CONFIG_ENV = "ASHA_CONFIG"
ROOT_ENV = "ASHA_PROJECTS_ROOT"
MAX_ROOTS = 8


def configured_roots(env: Mapping[str, str] | None = None) -> list[str]:
    """`project_roots` from the user's cross-project config, or an empty list.

    `~/.asha/config.json` is where user-owned, cross-project facts already live
    — `bin/asha` reads `default_harness` from it on every bare launch — so a
    list of the directories someone keeps work in belongs there too. Unreadable
    or malformed config is not an error: the caller falls back to the ordinary
    single-root behaviour.
    """
    values = os.environ if env is None else env
    home = values.get("HOME") or str(Path.home())
    path = Path(values.get(USER_CONFIG_ENV) or Path(home) / ".asha" / "config.json")
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    roots = config.get("project_roots") if isinstance(config, dict) else None
    if not isinstance(roots, list):
        return []
    cleaned: list[str] = []
    for item in roots[:MAX_ROOTS]:
        if isinstance(item, str) and item.strip():
            cleaned.append(item.strip())
    return cleaned


def resolve_roots(
    explicit: list[str] | None = None, *, env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
) -> tuple[list[str], str]:
    """Which roots to index, and where that answer came from.

    Explicit beats ambient, per-invocation beats persistent:
    `--root` -> `ASHA_PROJECTS_ROOT` -> configured `project_roots` -> cwd.
    """
    values = os.environ if env is None else env
    if explicit:
        return list(explicit[:MAX_ROOTS]), "argument"
    ambient = (values.get(ROOT_ENV) or "").strip()
    if ambient:
        return [ambient], "environment"
    configured = configured_roots(values)
    if configured:
        return configured, "configuration"
    return [str(cwd or Path.cwd())], "cwd"


def list_projects_across(
    roots: list[str], *, depth: int = 1, match: str | None = None,
    source_of_roots: str = "argument",
) -> dict[str, Any]:
    """Index several roots into one closed payload, keeping each root's group.

    A root that cannot be indexed is reported in `skipped` rather than failing
    the whole listing: one missing directory in a configured list must not hide
    the projects in the others.
    """
    groups: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in roots[:MAX_ROOTS]:
        try:
            payload = list_projects(Path(item), depth=depth, match=match)
        except (ProjectIndexError, OSError) as exc:
            skipped.append({"root": str(item), "reason": str(exc)})
            continue
        projects = [entry for entry in payload["projects"] if entry["root"] not in seen]
        seen.update(entry["root"] for entry in projects)
        groups.append({
            "root": payload["root"], "source": payload["source"], "projects": projects,
        })
    everything = [entry for group in groups for entry in group["projects"]]
    return {
        "contract": PROJECT_LIST_CONTRACT,
        "root": groups[0]["root"] if len(groups) == 1 else None,
        "roots_from": source_of_roots,
        "source": groups[0]["source"] if len(groups) == 1 else "discovery",
        "match": match,
        "groups": groups,
        "projects": everything,
        "skipped": skipped,
    }


__all__ = [
    "PROJECT_LIST_CONTRACT", "ProjectIndexError", "configured_roots", "display_name",
    "list_projects", "list_projects_across", "resolve_roots",
]
