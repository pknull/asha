#!/usr/bin/env python3
"""Generate OKF index.md files for every directory of a bundle.

Each directory holding concepts gets an index.md with no frontmatter: a heading,
a Concepts section listing `[title](./file.md): description` for every
non-reserved .md file, and a Directories section linking child indexes (no
counts, so an enclosing index never goes stale when a nested writer saves).
The bundle-root index keeps its declared `okf_version` and its heading across
bare reruns; `--okf-version` declares or replaces the version. Output is
deterministic (no timestamps), so a second run is byte-identical and `--check`
can detect drift.

Usage:
    okf_index.py BUNDLE [--exclude GLOB]... [--title TEXT] [--okf-version V] [--check]
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

try:
    from okf_io import RESERVED, atomic_write, effective_excludes, iter_markdown, read_scalars, split_frontmatter
except ImportError:  # run as a script from another cwd
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from okf_io import RESERVED, atomic_write, effective_excludes, iter_markdown, read_scalars, split_frontmatter


def concept_entry(path: Path, data: dict) -> str:
    title = data.get("title") if isinstance(data.get("title"), str) and data.get("title").strip() else path.stem
    description = data.get("description") if isinstance(data.get("description"), str) else ""
    description = " ".join(description.split())
    line = f"- [{title}](./{path.name})"
    return f"{line}: {description}" if description else line


def render_directory_index(directory: Path, files: list[Path], children: dict[Path, int], *,
                           title: str, frontmatter: str | None = None) -> str:
    sections = [f"# {title}"]
    concepts = [f for f in files if f.parent == directory and f.name not in RESERVED]
    if concepts:
        entries = []
        for path in sorted(concepts, key=lambda p: p.name):
            try:
                data = read_scalars(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError):
                data = {}
            entries.append(concept_entry(path, data))
        sections.append("## Concepts\n\n" + "\n".join(entries))
    if children:
        # No counts: a count changes on every save below and would make this
        # index stale whenever a nested writer adds or retires a concept.
        lines = [f"- [{child.name}/](./{child.name}/index.md)" for child in sorted(children, key=lambda p: p.name)]
        sections.append("## Directories\n\n" + "\n".join(lines))
    text = "\n\n".join(sections) + "\n"
    if frontmatter is not None:
        text = f"---\n{frontmatter}\n---\n" + text
    return text


_H1_RE = re.compile(r"^# (.+?)\s*$", re.MULTILINE)
_FENCE_RE = re.compile(r"^[ \t]*(`{3,}|~{3,})[^\n]*\n.*?(?:^[ \t]*\1[ \t]*$|\Z)", re.DOTALL | re.MULTILINE)
_UNLINKABLE_RE = re.compile(r"[\s#?()<>]")


def build_indexes(root: Path, excludes=(), title: str | None = None, okf_version: str | None = None) -> dict[Path, str]:
    """Return {index_path: content} for every directory that holds concepts.

    `excludes` are merged with the bundle's .okfignore. The root index keeps an
    existing `okf_version` block (or gets one from `okf_version`) and, when no
    title is given, keeps its existing heading, so a later run with no
    arguments reproduces what the first run wrote.
    """
    root = Path(root)
    excludes = effective_excludes(root, excludes)
    all_files = iter_markdown(root, excludes)
    files = [f for f in all_files if f.name not in RESERVED]
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
    for existing in (f for f in all_files if f.name == "index.md"):
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
        heading = directory.name
        if directory == root:
            existing = root / "index.md"
            existing_text = existing.read_text(encoding="utf-8") if existing.is_file() else ""
            _, body = split_frontmatter(existing_text)
            # Only okf_version may live here (spec §8); anything else an earlier hand
            # wrote is dropped rather than reproduced into a validator error. An
            # explicit --okf-version wins over the declared one.
            declared = read_scalars(existing_text).get("okf_version")
            version = okf_version or (str(declared) if declared not in (None, "") else None)
            if version:
                frontmatter = f"okf_version: {__import__('json').dumps(str(version))}"
            if title:
                heading = title
            else:
                match = _H1_RE.search(_FENCE_RE.sub("\n", body))
                if match:
                    heading = match.group(1)
        result[directory / "index.md"] = render_directory_index(
            directory, files, children, title=heading, frontmatter=frontmatter)
    return result


def unlinkable(root: Path, excludes=()) -> list[Path]:  # excludes already merged by callers
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
    return set(re.findall(r"\]\((\./[^)\s]+)\)", content))


def write_indexes(root: Path, excludes=(), title: str | None = None, okf_version: str | None = None) -> list[Path]:
    written = []
    for path, content in build_indexes(root, excludes, title, okf_version).items():
        if not path.is_file() or path.read_text(encoding="utf-8") != content:
            atomic_write(path, content)
            written.append(path)
    return written


def check_indexes(root: Path, excludes=(), title: str | None = None, okf_version: str | None = None) -> list[Path]:
    """Index files that are missing or differ from what build_indexes would write."""
    stale = []
    for path, content in build_indexes(root, excludes, title, okf_version).items():
        if not path.is_file() or path.read_text(encoding="utf-8") != content:
            stale.append(path)
    return stale


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Generate OKF index.md files for a bundle.")
    parser.add_argument("bundle")
    parser.add_argument("--exclude", action="append", default=[], help="bundle-relative glob or dir/ prefix to skip")
    parser.add_argument("--title", default=None, help="heading for the root index (default: keep the existing one, else the directory name)")
    parser.add_argument("--okf-version", default=None, help="declare okf_version on the root index (e.g. 0.2); an explicit value replaces a declared one")
    parser.add_argument("--check", action="store_true", help="report stale indexes and exit 1 instead of writing")
    args = parser.parse_args(argv)
    root = Path(args.bundle)
    if not root.is_dir():
        print(f"error: not a directory: {root}", file=sys.stderr)
        return 2
    if args.check:
        stale = check_indexes(root, args.exclude, args.title, args.okf_version)
        for path in stale:
            print(f"STALE  {path.relative_to(root).as_posix()}")
        print(f"{len(stale)} stale index file(s)")
        return 1 if stale else 0
    written = write_indexes(root, args.exclude, args.title, args.okf_version)
    for path in written:
        print(f"WROTE  {path.relative_to(root).as_posix()}")
    for path in unlinkable(root, effective_excludes(root, args.exclude)):
        print(f"UNLINKABLE  {path.relative_to(root).as_posix()}: rename; a markdown link cannot address this name")
    print(f"{len(written)} index file(s) written")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
