#!/usr/bin/env python3
"""Strict reader/writer contract for task-local Asha Control ownership."""

from __future__ import annotations

import json
import os
import re
import stat
import unicodedata
import uuid
from pathlib import Path
from typing import Any


MARKER_CONTRACT = "asha.control-task-context.v1"
MAX_MARKER_BYTES = 32 * 1024
_CHANGE_ID = re.compile(r"[k-z]{32}", re.ASCII)
_COMMIT_ID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", re.ASCII)
_WORKSPACE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", re.ASCII)
_REPOSITORY_ID = re.compile(r"repo:[0-9a-f]{64}", re.ASCII)


class MarkerError(ValueError):
    pass


class _DuplicateKey(ValueError):
    pass


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey(key)
        result[key] = value
    return result


def _exact_keys(value: Any, expected: set[str], name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise MarkerError(f"{name} must contain exactly the v1 fields")
    return value


def _canonical_path(value: Any, name: str) -> str:
    if (not isinstance(value, str) or not value.startswith("/") or
            value.startswith("//") or (value != "/" and value.endswith("/")) or
            os.path.normpath(value) != value or os.path.realpath(value) != value):
        raise MarkerError(f"{name} must be an exact canonical absolute path")
    if any(unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in value):
        raise MarkerError(f"{name} contains a Unicode control character")
    return value


def validate_marker(value: Any, *, workspace_root: Path | None = None) -> dict[str, Any]:
    marker = _exact_keys(
        value, {"contract", "task_id", "repository", "jj"}, "Control task marker"
    )
    if marker["contract"] != MARKER_CONTRACT:
        raise MarkerError(f"Control task marker contract must be {MARKER_CONTRACT}")
    try:
        parsed = uuid.UUID(marker["task_id"])
    except (ValueError, AttributeError, TypeError) as exc:
        raise MarkerError("Control task marker task_id must be a canonical UUID") from exc
    if str(parsed) != marker["task_id"]:
        raise MarkerError("Control task marker task_id must be a canonical UUID")
    repository = _exact_keys(marker["repository"], {"root", "identity"}, "marker repository")
    _canonical_path(repository["root"], "marker repository root")
    if not isinstance(repository["identity"], str) or _REPOSITORY_ID.fullmatch(repository["identity"]) is None:
        raise MarkerError("marker repository identity is invalid")
    jj = _exact_keys(
        marker["jj"],
        {"workspace_name", "workspace_path", "change_id", "working_commit_id"},
        "marker jj",
    )
    if not isinstance(jj["workspace_name"], str) or _WORKSPACE_NAME.fullmatch(jj["workspace_name"]) is None:
        raise MarkerError("marker jj workspace_name is invalid")
    workspace_path = _canonical_path(jj["workspace_path"], "marker jj workspace_path")
    if workspace_root is not None and workspace_path != str(workspace_root):
        raise MarkerError("marker jj workspace_path does not match marker location")
    if not isinstance(jj["change_id"], str) or _CHANGE_ID.fullmatch(jj["change_id"]) is None:
        raise MarkerError("marker jj change_id is invalid")
    if not isinstance(jj["working_commit_id"], str) or _COMMIT_ID.fullmatch(jj["working_commit_id"]) is None:
        raise MarkerError("marker jj working_commit_id is invalid")
    return marker


def canonical_marker_bytes(value: dict[str, Any], *, workspace_root: Path) -> bytes:
    validated = validate_marker(value, workspace_root=workspace_root)
    raw = json.dumps(validated, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
    if len(raw) > MAX_MARKER_BYTES:
        raise MarkerError(f"Control task marker exceeds {MAX_MARKER_BYTES} bytes")
    return raw


def _reject_symlink_components(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(mode):
            raise MarkerError(f"symlink rejected in Control task marker path: {current}")


def read_marker(path: Path, *, workspace_root: Path) -> dict[str, Any]:
    path = Path(path)
    _reject_symlink_components(path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise MarkerError(f"cannot open Control task marker: {exc}") from exc
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise MarkerError("Control task marker must be one regular file")
        if metadata.st_uid != os.geteuid():
            raise MarkerError("Control task marker is not owned by the effective user")
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise MarkerError("Control task marker must have mode 0600")
        if metadata.st_size > MAX_MARKER_BYTES:
            raise MarkerError(f"Control task marker exceeds {MAX_MARKER_BYTES} bytes")
        chunks: list[bytes] = []
        remaining = MAX_MARKER_BYTES + 1
        while remaining:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > MAX_MARKER_BYTES:
            raise MarkerError(f"Control task marker exceeds {MAX_MARKER_BYTES} bytes")
    finally:
        os.close(fd)
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_strict_object)
    except (_DuplicateKey, UnicodeError, json.JSONDecodeError) as exc:
        raise MarkerError("Control task marker is not strict UTF-8 JSON") from exc
    return validate_marker(value, workspace_root=workspace_root)


def find_marker(start: Path) -> tuple[Path, dict[str, Any]] | None:
    try:
        current = Path(start).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise MarkerError(f"cannot resolve save start path: {exc}") from exc
    if not current.is_dir():
        current = current.parent
    for root in (current, *current.parents):
        path = root / ".asha" / "control-task.json"
        try:
            path.lstat()
        except FileNotFoundError:
            continue
        return root, read_marker(path, workspace_root=root)
    return None
