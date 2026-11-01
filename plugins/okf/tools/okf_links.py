#!/usr/bin/env python3
"""Link resolution for OKF bundles in two grammars.

`standard`: markdown links to `.md` paths, as the spec writes them, with
percent-encoded targets decoded (a file named `Hot Springs Island.md` is
addressed as `./Hot%20Springs%20Island.md`; the upstream validator decodes
nothing, so this module owns the link checks whenever encoding is in play).

`obsidian`: the standard grammar plus `[[Note]]`, `[[Note|alias]]`,
`[[Note#heading]]` and `![[embed]]`, resolved the way Obsidian resolves them:
a target with a slash is a vault-relative path, a bare target is a basename
looked up across the bundle, unique or not. Ambiguity (two notes sharing a
basename) and missing targets are reported, never guessed.

The bundle's `.okfconfig` ({"links": "obsidian"}) selects the grammar for every
tool; `okf_validate.py --links` overrides it for one run.
"""
from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import quote, unquote

try:
    from okf_common import extract_links, is_external, path_part, strip_code
    from okf_io import RESERVED
except ImportError:  # run as a script from another cwd
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from okf_common import extract_links, is_external, path_part, strip_code
    from okf_io import RESERVED

WIKILINK_RE = re.compile(r"(!?)\[\[([^\]\|#]+)(#[^\]\|]*)?(\|[^\]]*)?\]\]")
_COMMENT_RE = re.compile(r"%%.*?%%|<!--.*?-->", re.DOTALL)  # Obsidian and HTML comments
GRAMMARS = ("standard", "obsidian")
_SAFE = "/-._~"
# Obsidian attachment types (plus the formats this vault embeds). A dotted bare
# name with any other suffix is a note name, so a missing one is reported.
ATTACHMENT_SUFFIXES = {"png", "jpg", "jpeg", "gif", "svg", "webp", "bmp", "avif", "pdf", "canvas", "base",
                       "excalidraw", "mp3", "wav", "ogg", "m4a", "flac", "mp4", "webm", "mov", "mkv", "ogv",
                       "zip", "csv", "json", "txt", "yml", "yaml", "html", "css", "js"}


def encode_target(relative: str) -> str:
    """Percent-encode a bundle-relative path for a markdown link target."""
    return quote(relative, safe=_SAFE)


def decode_target(target: str) -> str:
    return unquote(target)


def wikilinks(text: str) -> list[tuple[str, bool]]:
    """(target, is_embed) for every wikilink outside fenced and inline code.

    A `[[placeholder]]` inside a code block is documentation, not a link, in
    Obsidian's rendering and in the spec's grammar alike.
    """
    return [(m.group(2).strip(), m.group(1) == "!") for m in WIKILINK_RE.finditer(_COMMENT_RE.sub(" ", strip_code(text)))]


class Resolver:
    """Resolves link targets against every markdown file on disk under root.

    `files` are the bundle's admitted concepts; `all_files` (default: the same
    list) is what links may point at. A link into a file the bundle excludes is
    not broken: the note exists, it is simply not a concept of this bundle, so
    it earns no inbound credit and is never an orphan.
    """

    def __init__(self, root: Path, files: list[Path], grammar: str = "standard", all_files: list[Path] | None = None):
        if grammar not in GRAMMARS:
            raise ValueError(f"unknown link grammar: {grammar}")
        self.root = Path(root)
        self.grammar = grammar
        self.files = files
        self.admitted = set(files)
        universe = list(all_files) if all_files is not None else list(files)
        seen = set(universe)
        universe.extend(f for f in files if f not in seen)  # admitted paths always belong to the universe
        self.by_resolved: dict[Path, Path] = {}
        for f in sorted(universe, key=lambda f: (f not in self.admitted, f.relative_to(self.root).as_posix())):
            self.by_resolved.setdefault(f.resolve(), f)
        # Obsidian matches note names case-insensitively; so do the path forms here.
        self.by_rel = {}
        for f in universe:
            self.by_rel.setdefault(f.relative_to(self.root).as_posix().lower(), f)
        self.by_stem: dict[str, list[Path]] = {}
        for f in universe:
            self.by_stem.setdefault(f.stem.lower(), []).append(f)

    def _prefer_admitted(self, matches: list[Path]) -> list[Path]:
        """Excluded twins never make an admitted target ambiguous; they count only when nothing admitted matches."""
        admitted = [m for m in matches if m in self.admitted]
        return admitted or matches

    def _standard(self, target: str, source: Path):
        part = decode_target(path_part(target))
        if not part or not part.endswith(".md"):
            return None, None
        candidate = (self.root / part.lstrip("/")) if part.startswith("/") else (source.parent / part)
        try:
            resolved = candidate.resolve()
        except OSError:
            return None, f"unresolvable link '{target}'"
        found = self.by_resolved.get(resolved)
        if found is None:
            return None, f"broken link to '{target}'"
        return found, None

    def _wiki(self, target: str, source: Path):
        name = target.strip().rstrip("\\")
        if name.lower().endswith(".md"):
            name = name[:-3]
        elif "." in name.rsplit("/", 1)[-1] and not name.rsplit("/", 1)[-1].startswith("."):
            # A dotted bare name is an attachment (image, PDF, canvas...) unless a
            # note carries that whole name as its stem (`v1.2.md`, `workbench.archive.md`).
            suffix = name.rsplit(".", 1)[-1].lower()
            is_note = "/" not in name and name.lower() in self.by_stem
            if not is_note and suffix in ATTACHMENT_SUFFIXES:
                return None, None
        if "/" in name:
            rel = name.lstrip("/").lower() + ".md"
            found = self.by_rel.get(rel)
            if found is None:
                # A path relative to the source note, as Obsidian also accepts.
                try:
                    found = self.by_resolved.get((source.parent / (name + ".md")).resolve())
                except OSError:
                    found = None
            if found is None:
                # Obsidian's shortest-path form: any unique path suffix.
                tail = "/" + rel.lstrip("./")
                suffix_matches = self._prefer_admitted([f for r, f in self.by_rel.items() if ("/" + r).endswith(tail)])
                if len(suffix_matches) == 1:
                    return suffix_matches[0], None
                if len(suffix_matches) > 1:
                    names = ", ".join(sorted(m.relative_to(self.root).as_posix() for m in suffix_matches))
                    return None, f"ambiguous wikilink '[[{target}]]' matches {names}"
                return None, f"broken wikilink to '[[{target}]]'"
            return found, None
        matches = self._prefer_admitted(self.by_stem.get(name.lower(), []))
        # Two paths that are one file (a symlink alias) are one match, not twins.
        unique: dict[Path, Path] = {}
        for m in matches:
            unique.setdefault(m.resolve(), m)
        matches = list(unique.values())
        if not matches:
            return None, f"broken wikilink to '[[{target}]]'"
        if len(matches) > 1:
            # Asha's tie-break, not a documented Obsidian rule: a twin in the
            # source's own directory wins; otherwise the link is reported.
            same_dir = [m for m in matches if m.parent == source.parent]
            if len(same_dir) == 1:
                return same_dir[0], None
            names = ", ".join(sorted(m.relative_to(self.root).as_posix() for m in matches))
            return None, f"ambiguous wikilink '[[{target}]]' matches {names}"
        return matches[0], None

    def links_from(self, source: Path, text: str) -> tuple[set[Path], list[str]]:
        """Resolved targets of every intra-bundle link in text, and the problems."""
        targets: set[Path] = set()
        problems: list[str] = []
        for raw in extract_links(text):
            if is_external(raw) or raw.startswith("#"):
                continue
            found, problem = self._standard(raw, source)
            if found is not None:
                targets.add(found)
            elif problem:
                problems.append(problem)
        if self.grammar == "obsidian":
            for target, _embed in wikilinks(text):
                found, problem = self._wiki(target, source)
                if found is not None:
                    targets.add(found)
                elif problem:
                    problems.append(problem)
        return targets, problems


def _extension_problems(text: str) -> list[str]:
    """The upstream 'missing the .md extension' lint for intra-bundle standard links."""
    problems = []
    for raw in extract_links(text):
        if is_external(raw) or raw.startswith("#"):
            continue
        part = decode_target(path_part(raw))
        if part and not part.endswith("/") and not part.endswith(".md") and (
                part.startswith("/") or part.startswith("./") or part.startswith("../")):
            if "." not in part.rsplit("/", 1)[-1]:
                problems.append(f"intra-bundle link '{raw}' is missing the .md extension")
    return problems


def analyze(root: Path, files: list[Path], grammar: str = "standard", all_files: list[Path] | None = None) -> dict:
    """Link graph facts for the strict gate: per-file problems, orphans, reachability.

    `all_files` is every markdown file under root regardless of the ignore
    list; links resolve against it, concepts and orphans come from `files`.
    """
    root = Path(root)
    resolver = Resolver(root, files, grammar, all_files)
    admitted = set(files)
    # Two admitted paths may be one file (a symlink alias): the graph is keyed
    # on realpath so a link to either alias credits both.
    real = {f: f.resolve() for f in files}
    admitted_real = set(real.values())
    texts: dict[Path, str] = {}
    for f in files:
        try:
            texts[f] = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
    problems: list[tuple[str, str]] = []
    inbound: set[Path] = set()
    edges: dict[Path, set[Path]] = {}
    for f, text in texts.items():
        targets, file_problems = resolver.links_from(f, text)
        # Aliases of one file each contribute their outgoing links: union, never assign.
        outgoing = {t.resolve() for t in targets if t.resolve() in admitted_real}
        edges.setdefault(real[f], set()).update(outgoing)
        if f.name != "log.md":  # historical log links are expected to dangle
            if grammar == "obsidian":
                file_problems = file_problems + _extension_problems(text)
            problems.extend((f.relative_to(root).as_posix(), p) for p in file_problems)
            inbound.update(outgoing)
    orphans = [f for f in files if f.name not in RESERVED and real[f] not in inbound]
    reachable: set[Path] = set()
    root_index = root / "index.md"
    if root_index in texts:
        queue = [real[root_index]]
        reachable.add(real[root_index])
        while queue:
            current = queue.pop()
            for target in edges.get(current, ()):
                if target not in reachable:
                    reachable.add(target)
                    queue.append(target)
    unreachable = [f for f in files if f.name not in RESERVED and root_index in texts and real[f] not in reachable]
    return {"problems": problems, "orphans": orphans, "unreachable": unreachable,
            "has_root_index": root_index in texts, "grammar": grammar}
