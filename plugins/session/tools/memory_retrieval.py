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
import warnings
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable, Optional

import project_root


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


def _contained_path(candidate: Path, root: Path) -> Optional[Path]:
    try:
        resolved = candidate.resolve()
        canonical_root = root.resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    if resolved == canonical_root or canonical_root in resolved.parents:
        return resolved
    return None


def _workspace_warning(message: str) -> None:
    warnings.warn(f"workspace memory discovery: {message}", RuntimeWarning,
                  stacklevel=2)


def memory_entries(memory_dirs: Iterable[Path], *, source: str = "memory",
                   containment_root: Optional[Path] = None) -> list[Entry]:
    """Read MEMORY.md catalogue lines and target-file descriptions."""
    found: dict[tuple[str, str], Entry] = {}
    for memory_dir in memory_dirs:
        index = memory_dir / "MEMORY.md"
        indexed: dict[str, tuple[str, Path]] = {}
        try:
            safe_index: Optional[Path] = index.resolve()
        except (OSError, RuntimeError, ValueError):
            safe_index = None
        if containment_root is not None:
            safe_index = _contained_path(index, containment_root)  # type: ignore[assignment]
            if safe_index is None:
                _workspace_warning(f"skipping catalogue outside operational root: {index}")
        if safe_index is not None and safe_index.is_file():
            try:
                for line in safe_index.read_text(encoding="utf-8").splitlines():
                    match = _MEMORY_LINE_RE.match(line)
                    if not match:
                        continue
                    target = (memory_dir / match.group(2)).resolve()
                    if containment_root is not None:
                        target = _contained_path(target, containment_root)  # type: ignore[assignment]
                        if target is None:
                            _workspace_warning(
                                f"skipping catalogue target outside operational root: "
                                f"{match.group(2)}"
                            )
                            continue
                    indexed[target.stem] = (
                        " ".join(part for part in (match.group(1), match.group(3)) if part),
                        target,
                    )
            except (OSError, UnicodeError):
                pass

        targets = {path for _, path in indexed.values()}
        try:
            for path in memory_dir.glob("*.md"):
                if path.name == "MEMORY.md":
                    continue
                resolved = path.resolve()
                if containment_root is not None:
                    resolved = _contained_path(resolved, containment_root)  # type: ignore[assignment]
                    if resolved is None:
                        _workspace_warning(
                            f"skipping target outside operational root: {path}"
                        )
                        continue
                targets.add(resolved)
        except OSError:
            pass
        for path in sorted(targets):
            catalogue, _ = indexed.get(path.stem, (path.stem, path))
            description = ""
            try:
                description = str(_frontmatter(path.read_text(encoding="utf-8")).get("description") or "")
            except (OSError, UnicodeError):
                pass
            text = " ".join(part for part in (catalogue, description) if part)
            if text:
                found[(path.stem, str(path))] = _entry(path.stem, text, path, source)
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


def discover_retrieval_sources(
    project_dir: Optional[Path], *, all_projects: bool = False,
    home: Optional[Path] = None,
) -> tuple[list[Path], Optional[Path]]:
    """Return legacy memory dirs plus one contained workspace source.

    The legacy directory discovery stays byte-identical outside workspaces.
    A workspace operational plane is removed from that list and returned
    separately so it is classified once as ``workspace``.
    """
    memory_dirs = discover_memory_dirs(
        project_dir, all_projects=all_projects, home=home
    )
    if project_dir is None:
        return memory_dirs, None
    det = project_root.detect_workspace(start=project_dir)
    if det.errors:
        _workspace_warning(
            "detection failed; workspace source disabled (" +
            ", ".join(error.code for error in det.errors) + ")"
        )
        return memory_dirs, None
    if det.root is None or det.manifest is None:
        return memory_dirs, None

    try:
        canonical_root = det.root.resolve()
        rel = str((det.manifest.get("memory") or {}).get(
            "operational_root") or "Memory")
        operational = (det.root / rel).resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        _workspace_warning(f"cannot resolve operational root: {exc}")
        return memory_dirs, None
    if operational != canonical_root and canonical_root not in operational.parents:
        _workspace_warning(
            f"operational root resolves outside the workspace: {operational}"
        )
        return memory_dirs, None
    if not operational.is_dir():
        return memory_dirs, None

    memory_dirs = [path for path in memory_dirs if path.resolve() != operational]
    return memory_dirs, operational


def build_entries(memory_dirs: Iterable[Path], learnings_dir: Optional[Path] = None,
                  *, workspace_dir: Optional[Path] = None) -> list[Entry]:
    entries = memory_entries(memory_dirs)
    if workspace_dir is not None:
        entries.extend(memory_entries(
            [workspace_dir], source="workspace",
            containment_root=workspace_dir,
        ))
    entries.extend(learning_entries(learnings_dir or (Path.home() / ".asha" / "learnings")))
    return entries


def source_signature(memory_dirs: Iterable[Path], learnings_dir: Path, *,
                     workspace_dir: Optional[Path] = None) -> dict[str, int]:
    """Compact mtime/size signature used to skip unchanged SessionStart builds."""
    memory_dirs = list(memory_dirs)
    paths: list[Path] = []
    for directory in memory_dirs:
        try:
            paths.extend(directory.glob("*.md"))
        except OSError:
            pass
    if learnings_dir.is_dir():
        paths.extend(learnings_dir.glob("*.md"))
    if workspace_dir is not None:
        try:
            index = _contained_path(workspace_dir / "MEMORY.md", workspace_dir)
            if index is not None and index.is_file():
                paths.append(index)
                try:
                    for line in index.read_text(encoding="utf-8").splitlines():
                        match = _MEMORY_LINE_RE.match(line)
                        if not match:
                            continue
                        target = _contained_path(
                            workspace_dir / match.group(2), workspace_dir
                        )
                        if target is None:
                            _workspace_warning(
                                "skipping signature catalogue target outside "
                                f"operational root: {match.group(2)}"
                            )
                            continue
                        paths.append(target)
                except (OSError, UnicodeError):
                    pass
            for path in workspace_dir.glob("*.md"):
                resolved = _contained_path(path, workspace_dir)
                if resolved is None:
                    _workspace_warning(
                        f"skipping signature target outside operational root: {path}"
                    )
                    continue
                paths.append(resolved)
        except OSError:
            pass
    signature: dict[str, int] = {}
    # File metadata alone cannot distinguish the same directory changing from
    # project memory to the workspace operational plane (or back). Cache the
    # source topology as part of the signature so Entry.source never survives
    # a manifest/classification transition stale.
    for directory in memory_dirs:
        try:
            signature[f"@source:memory:{directory.resolve()}"] = 1
        except (OSError, RuntimeError):
            signature[f"@source:memory:{directory}"] = 1
    try:
        signature[f"@source:learning:{learnings_dir.resolve()}"] = 1
    except (OSError, RuntimeError):
        signature[f"@source:learning:{learnings_dir}"] = 1
    if workspace_dir is not None:
        try:
            signature[f"@source:workspace:{workspace_dir.resolve()}"] = 1
        except (OSError, RuntimeError):
            signature[f"@source:workspace:{workspace_dir}"] = 1
    for path in paths:
        try:
            stat = path.stat()
            signature[str(path)] = stat.st_mtime_ns ^ stat.st_size
        except OSError:
            pass
    return signature


# Broad-entry scrutiny (harness memory-selector discipline, ported lexically):
# a sprawling catalogue line has more surface to collide with any query, so
# breadth both discounts the score (BM25-style, _BM25_B) and — in the nudge's
# firing gate — disqualifies weak single-token evidence entirely.
BROAD_ENTRY_TOKENS = 25
_BM25_B = 0.4


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
    avg_breadth = sum(len(set(item.tokens)) for item in entries) / total

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
        # Mild length normalization: entries at average breadth keep their
        # score; broader ones are discounted, narrower ones lifted (b=0.4).
        breadth_norm = 1.0
        if avg_breadth > 0:
            breadth_norm = 1.0 / (1.0 - _BM25_B + _BM25_B * (len(item_set) / avg_breadth))
        score = (overlap_weight / denominator) * breadth_norm
        normalized_desc = " ".join(item.tokens)
        # Exact-phrase containment is strong aboutness evidence — the bonus
        # stays un-normalized on purpose.
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
            "entry_tokens": len(item_set),
            "corpus_size": total,
        })
    def source_rank(source: str) -> int:
        # Existing and unknown sources keep the legacy tie behavior. Only the
        # newly ratified workspace source sorts after them on an exact tie.
        return 1 if source == "workspace" else 0

    results.sort(key=lambda row: (
        -row["score"], -len(row["overlap"]), source_rank(row["source"]),
        row["id"], row["path"],
    ))
    return results[:limit]


def dump_index(path: Path, memory_dirs: list[Path], learnings_dir: Path, *,
               workspace_dir: Optional[Path] = None) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    lock_fd = os.open(lock_path, flags, 0o600)
    os.fchmod(lock_fd, 0o600)
    with os.fdopen(lock_fd, "a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        signature = source_signature(
            memory_dirs, learnings_dir, workspace_dir=workspace_dir
        )
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
            "entries": [entry.json() for entry in build_entries(
                memory_dirs, learnings_dir, workspace_dir=workspace_dir
            )],
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
