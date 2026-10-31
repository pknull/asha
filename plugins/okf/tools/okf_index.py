#!/usr/bin/env python3
"""Generate OKF index.md files for every directory of a bundle.

Each directory holding concepts gets an index.md with no frontmatter: a heading,
a Concepts section listing `[title](./file.md): description` for every
non-reserved .md file, and a Directories section linking child indexes. The
bundle-root index keeps an existing `okf_version` frontmatter block when one is
present and never adds one. Output is deterministic (no timestamps), so a
second run is byte-identical and `--check` can detect drift.

Usage:
    okf_index.py BUNDLE [--exclude GLOB]... [--title TEXT] [--check]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

try:
    from okf_io import RESERVED, atomic_write, is_excluded, iter_markdown, read_scalars, split_frontmatter
except ImportError:  # run as a script from another cwd
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from okf_io import RESERVED, atomic_write, is_excluded, iter_markdown, read_scalars, split_frontmatter


def concept_entry(path: Path, data: dict) -> str:
    title = data.get("title") if isinstance(data.get("title"), str) and data.get("title").strip() else path.stem
    description = data.get("description") if isinstance(data.get("description"), str) else ""
    description = " ".join(description.split())
    line = f"- [{title}](./{path.name})"
    return f"{line}: {description}" if description else line


def render_directory_index(directory: Path, files: list[Path], children: dict[Path, int], *,
                           title: str, frontmatter: str | None = None) -> str:
    out = []
    if frontmatter is not None:
        out.append(f"---\n{frontmatter}\n---\n")
    out.append(f"# {title}")
    concepts = [f for f in files if f.parent == directory and f.name not in RESERVED]
    if concepts:
        out.append("\n## Concepts\n")
        for path in sorted(concepts, key=lambda p: p.name):
            try:
                data = read_scalars(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError):
                data = {}
            out.append(concept_entry(path, data))
        out.append("")
    if children:
        out.append("\n## Directories\n")
        for child in sorted(children, key=lambda p: p.name):
            count = children[child]
            noun = "concept" if count == 1 else "concepts"
            out.append(f"- [{child.name}/](./{child.name}/index.md): {count} {noun}")
        out.append("")
    text = "\n".join(out)
    return text if text.endswith("\n") else text + "\n"


def build_indexes(root: Path, excludes=(), title: str | None = None) -> dict[Path, str]:
    """Return {index_path: content} for every directory that holds concepts."""
    root = Path(root)
    files = [f for f in iter_markdown(root, excludes) if f.name not in RESERVED]
    counts: dict[Path, int] = {}
    for path in files:
        cursor = path.parent
        while True:
            counts[cursor] = counts.get(cursor, 0) + 1
            if cursor == root:
                break
            cursor = cursor.parent
    # A directory that already carries an index keeps one when its last concept
    # leaves, so a drained directory never holds links to files that are gone.
    for existing in root.rglob("index.md"):
        rel = existing.relative_to(root)
        if any(part.startswith(".") for part in rel.parts[:-1]) or is_excluded(rel.as_posix(), excludes):
            continue
        cursor = existing.parent
        while True:
            counts.setdefault(cursor, 0)
            if cursor == root:
                break
            cursor = cursor.parent
    counts.setdefault(root, 0)
    result: dict[Path, str] = {}
    for directory in counts:
        children = {child: counts[child] for child in counts if child.parent == directory and child != directory}
        frontmatter = None
        if directory == root:
            existing = root / "index.md"
            if existing.is_file():
                block, _ = split_frontmatter(existing.read_text(encoding="utf-8"))
                if block is not None and "okf_version" in block:
                    frontmatter = block
        heading = title if (directory == root and title) else directory.name
        result[directory / "index.md"] = render_directory_index(
            directory, files, children, title=heading, frontmatter=frontmatter)
    return result


_UNLINKABLE_RE = __import__("re").compile(r"[\s#?()<>]")


def unlinkable(root: Path, excludes=()) -> list[Path]:
    """Concept files whose names a standard markdown link cannot address.

    The spec's link grammar stops a target at whitespace or ')' and treats '#'
    and '?' as fragment and query; the upstream validator decodes nothing, so a
    percent-encoded link would read as broken. Such files are indexed but will
    lint; rename them.
    """
    return [p for p in iter_markdown(Path(root), excludes)
            if p.name not in RESERVED and _UNLINKABLE_RE.search(p.name)]


def index_links(content: str) -> set[str]:
    """The `./target` link targets an index content names."""
    import re
    return set(re.findall(r"\]\((\./[^)\s]+)\)", content))


def write_indexes(root: Path, excludes=(), title: str | None = None) -> list[Path]:
    written = []
    for path, content in build_indexes(root, excludes, title).items():
        if not path.is_file() or path.read_text(encoding="utf-8") != content:
            atomic_write(path, content)
            written.append(path)
    return written


def check_indexes(root: Path, excludes=(), title: str | None = None) -> list[Path]:
    """Index files that are missing or differ from what build_indexes would write."""
    stale = []
    for path, content in build_indexes(root, excludes, title).items():
        if not path.is_file() or path.read_text(encoding="utf-8") != content:
            stale.append(path)
    return stale


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Generate OKF index.md files for a bundle.")
    parser.add_argument("bundle")
    parser.add_argument("--exclude", action="append", default=[], help="bundle-relative glob or dir/ prefix to skip")
    parser.add_argument("--title", default=None, help="heading for the root index (default: directory name)")
    parser.add_argument("--check", action="store_true", help="report stale indexes and exit 1 instead of writing")
    args = parser.parse_args(argv)
    root = Path(args.bundle)
    if not root.is_dir():
        print(f"error: not a directory: {root}", file=sys.stderr)
        return 2
    if args.check:
        stale = check_indexes(root, args.exclude, args.title)
        for path in stale:
            print(f"STALE  {path.relative_to(root).as_posix()}")
        print(f"{len(stale)} stale index file(s)")
        return 1 if stale else 0
    written = write_indexes(root, args.exclude, args.title)
    for path in written:
        print(f"WROTE  {path.relative_to(root).as_posix()}")
    for path in unlinkable(root, args.exclude):
        print(f"UNLINKABLE  {path.relative_to(root).as_posix()}: rename; a markdown link cannot address this name")
    print(f"{len(written)} index file(s) written")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
