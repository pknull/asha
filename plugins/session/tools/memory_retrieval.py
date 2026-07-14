#!/usr/bin/env python3
"""Shared lexical retrieval substrate for recall benchmarks and memory nudges.

Only compact catalogue text is indexed: MEMORY.md entries, memory frontmatter
descriptions, and learning frontmatter titles/descriptions. Memory bodies are
never read into the index.
"""

from __future__ import annotations

import json
import fcntl
import math
import os
import re
import tempfile
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable, Optional


_WORD_RE = re.compile(r"[a-z0-9][a-z0-9_+.-]*", re.IGNORECASE)
_MEMORY_LINE_RE = re.compile(
    r"^\s*-\s*\[([^]]+)\]\(([^)]+\.md)\)\s*(?:[-–—:]\s*)?(.*)$"
)
_FRONTMATTER_RE = re.compile(r"^\ufeff?---\s*\r?\n(.*?)\r?\n---\s*(?:\r?\n|$)", re.DOTALL)
_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "been", "but", "by", "can",
    "do", "does", "for", "from", "had", "has", "have", "how", "i", "if",
    "in", "into", "is", "it", "its", "me", "my", "not", "of", "on", "or",
    "our", "should", "that", "the", "their", "then", "this", "to", "use",
    "was", "we", "what", "when", "where", "which", "with", "you", "your",
}


@dataclass(frozen=True)
class Entry:
    id: str
    description: str
    path: str
    source: str
    tokens: tuple[str, ...]

    def json(self) -> dict:
        value = asdict(self)
        value["tokens"] = list(self.tokens)
        return value


def tokenize(text: str) -> list[str]:
    """Stable tokenization shared by the benchmark and hook matcher."""
    values: list[str] = []
    for raw in _WORD_RE.findall(text.lower()):
        token = raw.strip("._+-")
        if len(token) < 2 or token in _STOPWORDS:
            continue
        # Keep both a dashed identifier and its components searchable.
        values.append(token)
        if "-" in token or "_" in token:
            values.extend(p for p in re.split(r"[-_]", token) if len(p) >= 2)
    return values


def _frontmatter(text: str) -> dict[str, object]:
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}
    # The fields used here are scalar strings. This small parser deliberately
    # avoids making PyYAML a runtime dependency for a latency-sensitive hook.
    data: dict[str, object] = {}
    current: Optional[str] = None
    for line in match.group(1).splitlines():
        if line[:1].isspace() and current and isinstance(data.get(current), str):
            data[current] = f"{data[current]} {line.strip()}".strip()
            continue
        if ":" not in line or line.lstrip().startswith("#"):
            current = None
            continue
        key, value = line.split(":", 1)
        current = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        data[current] = value
    return data


def _entry(entry_id: str, description: str, path: Path, source: str) -> Entry:
    clean = " ".join(description.split())
    return Entry(entry_id, clean, str(path), source, tuple(tokenize(clean)))


def memory_entries(memory_dirs: Iterable[Path]) -> list[Entry]:
    """Read MEMORY.md catalogue lines and target-file descriptions."""
    found: dict[tuple[str, str], Entry] = {}
    for memory_dir in memory_dirs:
        index = memory_dir / "MEMORY.md"
        indexed: dict[str, tuple[str, Path]] = {}
        if index.is_file():
            try:
                for line in index.read_text(encoding="utf-8").splitlines():
                    match = _MEMORY_LINE_RE.match(line)
                    if not match:
                        continue
                    target = (memory_dir / match.group(2)).resolve()
                    indexed[target.stem] = (
                        " ".join(part for part in (match.group(1), match.group(3)) if part),
                        target,
                    )
            except OSError:
                pass

        targets = {path for _, path in indexed.values()}
        try:
            targets.update(p.resolve() for p in memory_dir.glob("*.md") if p.name != "MEMORY.md")
        except OSError:
            pass
        for path in sorted(targets):
            catalogue, _ = indexed.get(path.stem, (path.stem, path))
            description = ""
            try:
                description = str(_frontmatter(path.read_text(encoding="utf-8")).get("description") or "")
            except OSError:
                pass
            text = " ".join(part for part in (catalogue, description) if part)
            if text:
                found[(path.stem, str(path))] = _entry(path.stem, text, path, "memory")
    return list(found.values())


def learning_entries(learnings_dir: Path) -> list[Entry]:
    """Read the same OKF scalar fields that learnings_manager indexes/renders."""
    if not learnings_dir.is_dir():
        return []
    entries: list[Entry] = []
    for path in sorted(learnings_dir.glob("*.md")):
        if path.name in {"index.md", "log.md"}:
            continue
        try:
            data = _frontmatter(path.read_text(encoding="utf-8"))
        except OSError:
            continue
        entry_id = str(data.get("id") or path.stem)
        # trigger is the historical learnings_manager query field. New OKF
        # files mirror it into description; keeping it here preserves old files.
        description = " ".join(str(data.get(k) or "") for k in ("title", "description", "trigger"))
        entries.append(_entry(entry_id, description, path, "learning"))
    return entries


def claude_memory_dir(project_dir: Path, home: Optional[Path] = None) -> Path:
    home = home or Path.home()
    key = str(project_dir.resolve()).replace(os.sep, "-")
    return home / ".claude" / "projects" / key / "memory"


def discover_memory_dirs(project_dir: Optional[Path], *, all_projects: bool = False,
                         home: Optional[Path] = None) -> list[Path]:
    home = home or Path.home()
    result: list[Path] = []
    override = os.environ.get("ASHA_MEMORY_DIR")
    if override:
        result.append(Path(override).expanduser())
    if project_dir:
        native = project_dir / "Memory"
        if (native / "MEMORY.md").is_file():
            result.append(native)
        result.append(claude_memory_dir(project_dir, home))
    if all_projects:
        # Deliberately scoped to Claude's project-memory catalogue. Never scan HOME.
        result.extend((home / ".claude" / "projects").glob("*/memory"))
    unique: dict[str, Path] = {}
    for path in result:
        if path.is_dir():
            unique[str(path.resolve())] = path.resolve()
    return sorted(unique.values())


def build_entries(memory_dirs: Iterable[Path], learnings_dir: Optional[Path] = None) -> list[Entry]:
    entries = memory_entries(memory_dirs)
    entries.extend(learning_entries(learnings_dir or (Path.home() / ".asha" / "learnings")))
    return entries


def source_signature(memory_dirs: Iterable[Path], learnings_dir: Path) -> dict[str, int]:
    """Compact mtime/size signature used to skip unchanged SessionStart builds."""
    paths: list[Path] = []
    for directory in memory_dirs:
        try:
            paths.extend(directory.glob("*.md"))
        except OSError:
            pass
    if learnings_dir.is_dir():
        paths.extend(learnings_dir.glob("*.md"))
    signature: dict[str, int] = {}
    for path in paths:
        try:
            stat = path.stat()
            signature[str(path)] = stat.st_mtime_ns ^ stat.st_size
        except OSError:
            pass
    return signature


def rank(query: str, entries: Iterable[Entry], limit: int = 5) -> list[dict]:
    entries = list(entries)
    query_tokens = tokenize(query)
    if not query_tokens or not entries:
        return []
    query_set = set(query_tokens)
    df: dict[str, int] = {}
    for item in entries:
        for token in set(item.tokens):
            df[token] = df.get(token, 0) + 1
    total = len(entries)

    def idf(token: str) -> float:
        return math.log((total + 1) / (df.get(token, 0) + 1)) + 1.0

    denominator = sum(idf(token) for token in query_set) or 1.0
    results: list[dict] = []
    normalized_query = " ".join(query_tokens)
    for item in entries:
        item_set = set(item.tokens)
        overlap = query_set & item_set
        if not overlap:
            continue
        overlap_weight = sum(idf(token) for token in overlap)
        score = overlap_weight / denominator
        normalized_desc = " ".join(item.tokens)
        if len(normalized_query) >= 5 and normalized_query in normalized_desc:
            score += 0.25
        results.append({
            "id": item.id,
            "description": item.description,
            "path": item.path,
            "source": item.source,
            "score": round(score, 6),
            "overlap": sorted(overlap),
            "overlap_idf": round(overlap_weight, 6),
            "max_overlap_idf": round(max(idf(token) for token in overlap), 6),
            "min_overlap_df": min(df.get(token, 0) for token in overlap),
            "corpus_size": total,
        })
    results.sort(key=lambda row: (-row["score"], -len(row["overlap"]), row["id"], row["path"]))
    return results[:limit]


def dump_index(path: Path, memory_dirs: list[Path], learnings_dir: Path) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    lock_fd = os.open(lock_path, flags, 0o600)
    os.fchmod(lock_fd, 0o600)
    with os.fdopen(lock_fd, "a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        signature = source_signature(memory_dirs, learnings_dir)
        if path.is_file() and not path.is_symlink():
            try:
                old = json.loads(path.read_text(encoding="utf-8"))
                if old.get("source_signature") == signature:
                    os.chmod(path, 0o600)
                    return old
            except (OSError, ValueError, TypeError):
                pass
        payload = {
            "version": 1,
            "source_signature": signature,
            "entries": [entry.json() for entry in build_entries(memory_dirs, learnings_dir)],
        }
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        tmp = Path(tmp_name)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        finally:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        return payload


def load_index(path: Path) -> list[Entry]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return [Entry(
        id=str(item["id"]), description=str(item["description"]),
        path=str(item["path"]), source=str(item["source"]),
        tokens=tuple(str(v) for v in item.get("tokens", [])),
    ) for item in data.get("entries", [])]
