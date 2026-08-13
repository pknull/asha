#!/usr/bin/env python3
"""Strict project-root containment for Memory v2 writes."""

from __future__ import annotations

import os
import stat
from pathlib import Path


def secure_project_root(project_dir: Path, *, reject_home: bool = False) -> Path:
    """Return an absolute project root only when no path component is a symlink."""
    supplied = Path(project_dir).expanduser()
    absolute = Path(os.path.abspath(supplied))
    if not absolute.exists() or not absolute.is_dir():
        raise ValueError(f"project root is not a directory: {absolute}")
    resolved = absolute.resolve(strict=True)
    if resolved != absolute:
        raise ValueError(f"project root contains a symlink: {absolute}")
    if reject_home and resolved == Path.home().resolve():
        raise ValueError("home directory is not a project recovery root")
    return resolved


def secure_path(root: Path, relative: Path | str, *, create_parents: bool = False) -> Path:
    """Resolve a project child without following any existing symlink component."""
    root = secure_project_root(root)
    relative = Path(relative)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"path escapes project root: {relative}")
    target = root / relative
    cursor = root
    for part in relative.parts:
        cursor = cursor / part
        try:
            mode = cursor.lstat().st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(mode):
            raise ValueError(f"symlinked write root rejected: {cursor}")
    if create_parents:
        target.parent.mkdir(parents=True, exist_ok=True)
        # Recheck after creation to close the ordinary pre-creation gap. The
        # publication lock handles cooperating writers; hostile replacement is
        # outside this local-user mechanism's threat model.
        cursor = root
        for part in relative.parent.parts:
            cursor = cursor / part
            if stat.S_ISLNK(cursor.lstat().st_mode):
                raise ValueError(f"symlinked write root rejected: {cursor}")
    resolved_parent = target.parent.resolve(strict=False)
    if not resolved_parent.is_relative_to(root):
        raise ValueError(f"path escapes project root: {target}")
    if target.exists() and target.resolve(strict=True).parent != resolved_parent:
        raise ValueError(f"symlinked target rejected: {target}")
    return target
