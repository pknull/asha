#!/usr/bin/env python3
"""Workspace v3 canonical-knowledge core.

This module creates and validates the configured canonical knowledge tree,
builds explicit promotion plans, and applies their file write-set through a
durable recovery journal. Pull-request plans have a separate, explicitly
confirmed publisher which creates a dedicated branch, stages only the reviewed
write-set, commits, pushes that branch, and opens a draft pull request. It never
merges or direct-pushes a canonical change.

The contract comes from epic #23 and the ratified workspace proposal:
``docs/proposals/2026-08-06--workspace-memory.md``.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Optional
from urllib.parse import quote, urlsplit

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import project_root  # noqa: E402


SCHEMA_VERSION = 1
OWNERSHIP_FILE = ".asha-owned.json"
INDEX_FILE = ".asha-index.json"
REPOSITORY_DOCUMENTS = (
    "projectbrief.md",
    "productContext.md",
    "systemPatterns.md",
    "techContext.md",
    "activeContext.md",
)
RESERVED_FILES = {OWNERSHIP_FILE, INDEX_FILE}
ALLOWED_TARGET_ROOTS = {"repos", "cross-cutting", "decisions", "tickets"}
_NO_PREIMAGE = object()

_LINK_RE = re.compile(r"!?\[[^]]*\]\(([^)]+)\)")
_EMAIL_RE = re.compile(r"(?<![\w.+-])[\w.+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![\w.-])")
_HOME_RE = re.compile(r"(?<![\w.-])/(?:home|Users)/[^/\s]+")
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?1[-. ]?)?\(?\d{3}\)?[-. ]\d{3}[-. ]\d{4}(?!\d)")
_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----", re.IGNORECASE
)
_TRANSCRIPT_RE = re.compile(
    r"(?im)^\s*(?:[\[{]\s*[\"']?role[\"']?\s*[:=]|role\s*:)\s*[\"']?"
    r"(?:user|assistant|system)\b"
)
_TRANSCRIPT_PATH_RE = re.compile(r"(?:transcript|conversation|chat[-_]?log|session[-_]?log)", re.I)
_TRANSIENT_LINE_RE = re.compile(
    r"(?i)^\s*(?:current branch|working tree|uncommitted|in[- ]flight|"
    r"session id|temporary blocker|transient state)\s*[:=-]"
)
_SECRET_ASSIGN_RE = re.compile(
    r"(?im)^(\s*(?:api[_-]?key|api[_-]?token|access[_-]?token|auth[_-]?token|"
    r"client[_-]?secret|password|passwd|secret|token)\s*[:=]\s*)"
    r"([^\s#][^\r\n#]*)"
)
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}")
_KNOWN_TOKEN_RE = re.compile(
    r"\b(?:gh[opusr]_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9_-]{20,}|"
    r"AKIA[0-9A-Z]{16})\b"
)
_CREDENTIAL_URL_RE = re.compile(r"(?i)(https?://)[^/@\s:]+:[^/@\s]+@")


def _issue(code: str, message: str, *, path: Optional[str] = None,
           severity: Optional[str] = None) -> dict[str, Any]:
    item: dict[str, Any] = {"code": code, "message": message}
    if path is not None:
        item["path"] = path
    if severity is not None:
        item["severity"] = severity
    return item


def _sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_bytes(data: Any) -> bytes:
    return (json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def _relative_path(value: str) -> Optional[str]:
    """Return a normalized safe POSIX relative path, or None."""
    if not isinstance(value, str) or not value or "\\" in value:
        return None
    pure = PurePosixPath(value)
    if pure.is_absolute() or any(part in ("", ".", "..") for part in pure.parts):
        return None
    return pure.as_posix()


def _contained_path(root: Path, relative: str) -> Optional[Path]:
    normalized = _relative_path(relative)
    if normalized is None:
        return None
    try:
        resolved_root = root.resolve()
        candidate = (root / normalized).resolve()
        candidate.relative_to(resolved_root)
        return candidate
    except (OSError, RuntimeError, ValueError):
        return None


def _lexical_contained_path(root: Path, relative: str) -> Optional[Path]:
    """Return the lexical path only when no existing component is a symlink."""
    normalized = _relative_path(relative)
    if normalized is None:
        return None
    lexical = root / normalized
    current = root
    for part in PurePosixPath(normalized).parts:
        current = current / part
        if current.is_symlink():
            return None
    try:
        lexical.resolve().relative_to(root.resolve())
    except (OSError, RuntimeError, ValueError):
        return None
    return lexical


def _inert_label(value: Any, *, limit: int = 160) -> str:
    """Render untrusted manifest labels as bounded, printable inert text."""
    raw = str(value)
    safe = "".join(ch if ch.isascii() and (ch.isalnum() or ch in " ._/-") else "_" for ch in raw)
    safe = re.sub(r"\s+", " ", safe).strip() or "workspace"
    return safe.encode("ascii", "ignore")[:limit].decode("ascii", "ignore").rstrip() or "workspace"


def _workspace_context(start: Path | str) -> tuple[Optional[dict[str, Any]], list[dict[str, Any]]]:
    det = project_root.detect_workspace(start=Path(start))
    if det.errors:
        return None, [e._asdict() for e in det.errors]
    if det.root is None or det.manifest is None:
        return None, [_issue(
            "no_workspace",
            "no .asha/workspace.json in any ancestor; canonical knowledge is never created implicitly",
        )]
    ws_root = det.root.resolve()
    manifest = det.manifest
    shared_rel = manifest.get("memory", {}).get("shared_root", "knowledge")
    try:
        shared_lexical = ws_root / shared_rel
        shared_root = shared_lexical.resolve()
        shared_root.relative_to(ws_root)
    except (OSError, RuntimeError, ValueError):
        return None, [_issue(
            "shared_root_escape",
            f"shared_root ({shared_rel}) resolves outside the workspace root",
            path=str(ws_root / shared_rel),
        )]
    return {
        "workspace_root": ws_root,
        "manifest": manifest,
        "shared_root": shared_root,
        "shared_lexical": shared_lexical,
        "shared_rel": shared_rel,
    }, []


def _title_from_name(value: str) -> str:
    stem = Path(value).stem
    words = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", stem).replace("-", "_").split("_")
    return " ".join(word.capitalize() for word in words if word)


def _frontmatter_document(title: str, doc_type: str, body: str,
                          *, repository: Optional[str] = None,
                          updated: Optional[str] = None) -> bytes:
    lines = [
        "---",
        f"title: {title}",
        f"type: {doc_type}",
        f"updated: {updated or date.today().isoformat()}",
    ]
    if repository:
        lines.append(f"repository: {repository}")
    lines.extend(["---", "", f"# {title}", "", body.rstrip(), ""])
    return "\n".join(lines).encode("utf-8")


def _layout_spec(ctx: dict[str, Any], include_tickets: bool) -> tuple[list[str], dict[str, bytes], list[str]]:
    manifest = ctx["manifest"]
    name = manifest.get("workspace_name", "workspace")
    display_name = _inert_label(name)
    repos = [entry["path"] for entry in manifest.get("repositories", [])]
    directories = ["repos", "cross-cutting", "decisions"]
    if include_tickets:
        directories.append("tickets")
    files: dict[str, bytes] = {}
    docs: list[str] = []
    navigation = [
        f"# {display_name} knowledge",
        "",
        "Canonical workspace knowledge. Changes are explicit and review-governed.",
        "",
        "## Repositories",
        "",
    ]
    for repo in repos:
        repo_label = _inert_label(repo)
        directories.append(f"repos/{repo}")
        navigation.append(f"- [{repo_label}]({quote(f'repos/{repo}/', safe='/._-')})")
        for filename in REPOSITORY_DOCUMENTS:
            rel = f"repos/{repo}/{filename}"
            title = f"{repo_label}: {_title_from_name(filename)}"
            files[rel] = _frontmatter_document(
                title, "repository-context", "No canonical content recorded yet.",
                repository=repo_label,
            )
            docs.append(rel)
    navigation.extend([
        "",
        "## Cross-cutting knowledge",
        "",
        "- [Cross-cutting](cross-cutting/)",
        "- [Decisions](decisions/)",
    ])
    if include_tickets:
        navigation.append("- [Tickets](tickets/)")
    navigation.append("")
    files["README.md"] = "\n".join(navigation).encode("utf-8")
    index = {
        "version": SCHEMA_VERSION,
        "workspace": display_name,
        "documents": sorted(docs),
    }
    files[INDEX_FILE] = _json_bytes(index)
    return sorted(set(directories)), files, sorted(docs)


def _read_json(path: Path) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return None, str(exc)
    if not isinstance(value, dict):
        return None, "root must be an object"
    return value, None


def _stage_temp(destination: Path, content: bytes) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    try:
        os.fchmod(fd, 0o644)
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        Path(tmp_name).unlink(missing_ok=True)
        raise
    return Path(tmp_name)


def _replace_file(source: Path, destination: Path) -> None:
    """Patch seam used by transaction failure tests."""
    os.replace(source, destination)


def _contained_parent_fd(root: Path, destination: Path) -> tuple[int, str]:
    """Open destination's parent beneath root without following symlinks."""
    try:
        rel = destination.absolute().relative_to(root.absolute())
    except ValueError as exc:
        raise OSError("destination escaped the transaction root") from exc
    if not rel.parts or any(part in {"", ".", ".."} for part in rel.parts):
        raise OSError("destination path is not a safe contained member")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(root, flags)
    try:
        for part in rel.parts[:-1]:
            next_fd = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd, rel.name
    except Exception:
        os.close(fd)
        raise


def _replace_dirfd(source_name: str, destination_name: str, parent_fd: int) -> None:
    """Patch seam for the final rename within an already-open parent."""
    os.replace(source_name, destination_name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)


def _hash_dirfd_member(parent_fd: int, name: str) -> Optional[str]:
    try:
        fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
    except FileNotFoundError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("transaction member became a non-regular file")
        digest = hashlib.sha256()
        while True:
            chunk = os.read(fd, 64 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        return digest.hexdigest()
    finally:
        os.close(fd)


def _replace_contained(root: Path, destination: Path, content: bytes,
                       *, expected_sha: object = _NO_PREIMAGE) -> None:
    """Atomically replace one file relative to symlink-resistant directory FDs."""
    parent_fd, name = _contained_parent_fd(root, destination)
    temp_name = f".{name}.{hashlib.sha256(os.urandom(32)).hexdigest()}.tmp"
    temp_fd: Optional[int] = None
    try:
        try:
            existing = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None and not stat.S_ISREG(existing.st_mode):
            raise OSError("destination became a non-regular file")
        temp_fd = os.open(
            temp_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=parent_fd,
        )
        view = memoryview(content)
        while view:
            written = os.write(temp_fd, view)
            view = view[written:]
        os.fsync(temp_fd)
        os.close(temp_fd)
        temp_fd = None
        if expected_sha is not _NO_PREIMAGE \
                and _hash_dirfd_member(parent_fd, name) != expected_sha:
            raise OSError("transaction member changed after journaling")
        _replace_dirfd(temp_name, name, parent_fd)
        os.fsync(parent_fd)
    finally:
        if temp_fd is not None:
            os.close(temp_fd)
        try:
            os.unlink(temp_name, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
        os.close(parent_fd)


def _unlink_contained(root: Path, destination: Path,
                      *, expected_sha: object = _NO_PREIMAGE) -> None:
    parent_fd, name = _contained_parent_fd(root, destination)
    try:
        try:
            existing = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        if not stat.S_ISREG(existing.st_mode):
            raise OSError("recovery target became a non-regular file")
        if expected_sha is not _NO_PREIMAGE \
                and _hash_dirfd_member(parent_fd, name) != expected_sha:
            raise OSError("recovery target changed after journaling")
        os.unlink(name, dir_fd=parent_fd)
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)


def _restore_originals(originals: dict[Path, Optional[bytes]], applied: Iterable[Path]) -> None:
    for path in reversed(list(applied)):
        old = originals[path]
        if old is None:
            path.unlink(missing_ok=True)
            continue
        tmp = _stage_temp(path, old)
        os.replace(tmp, path)


def _atomic_write_set(write_set: dict[Path, bytes]) -> tuple[bool, Optional[str], dict[Path, Optional[bytes]]]:
    """Apply a write-set with process-local best-effort rollback, not crash atomicity."""
    staged: dict[Path, Path] = {}
    originals: dict[Path, Optional[bytes]] = {}
    applied: list[Path] = []
    try:
        for destination, content in write_set.items():
            originals[destination] = destination.read_bytes() if destination.exists() else None
            staged[destination] = _stage_temp(destination, content)
        for destination in sorted(staged, key=lambda p: str(p)):
            _replace_file(staged[destination], destination)
            applied.append(destination)
        return True, None, originals
    except OSError as exc:
        _restore_originals(originals, applied)
        return False, str(exc), originals
    finally:
        for tmp in staged.values():
            tmp.unlink(missing_ok=True)


def initialize_layout(start: Path | str, *, include_tickets: bool = False) -> dict[str, Any]:
    """Create only missing Asha-owned knowledge scaffolding.

    Existing user files and drifted formerly-owned files are preserved.  The
    ownership registry records hashes only for files actually created by this
    operation, making reruns deterministic and collision-safe.
    """
    ctx, errors = _workspace_context(start)
    report: dict[str, Any] = {
        "operation": "init", "ok": False, "workspace_root": None,
        "shared_root": None, "created": [], "updated": [],
        "collisions": [], "drifted": [], "errors": errors,
    }
    if ctx is None:
        return report
    root: Path = ctx["shared_root"]
    report["workspace_root"] = str(ctx["workspace_root"])
    report["shared_root"] = str(root)
    # An existing symlink at shared_root was resolved by context.  It is valid
    # only when its target remains inside the workspace; escaped targets were
    # already rejected.  Layout files receive the same per-target check below.
    directories, desired, _ = _layout_spec(ctx, include_tickets)
    ownership_path = root / OWNERSHIP_FILE
    old_ownership: dict[str, Any] = {"version": SCHEMA_VERSION, "files": {}}
    if ownership_path.exists():
        loaded, why = _read_json(ownership_path)
        if why or loaded is None or loaded.get("version") != SCHEMA_VERSION \
                or not isinstance(loaded.get("files"), dict):
            report["errors"] = [_issue(
                "ownership_invalid", f"reserved ownership metadata is invalid: {why or 'wrong schema'}",
                path=OWNERSHIP_FILE,
            )]
            return report
        old_ownership = loaded

    owned = dict(old_ownership.get("files", {}))
    write_set: dict[Path, bytes] = {}
    for rel, content in desired.items():
        target = _contained_path(root, rel)
        if target is None:
            report["errors"].append(_issue(
                "layout_target_escape", "layout target resolves outside shared_root", path=rel
            ))
            continue
        if target.exists():
            if not target.is_file() or target.is_symlink():
                report["collisions"].append(rel)
                continue
            prior_hash = owned.get(rel)
            current_hash = _sha_bytes(target.read_bytes())
            if prior_hash is None:
                report["collisions"].append(rel)
            elif prior_hash != current_hash:
                report["drifted"].append(rel)
            continue
        write_set[target] = content
        owned[rel] = _sha_bytes(content)
        report["created"].append(rel)

    if report["errors"]:
        report["created"] = []
        return report
    ownership = {
        "version": SCHEMA_VERSION,
        "owner": "asha-workspace-knowledge",
        "files": dict(sorted(owned.items())),
    }
    ownership_bytes = _json_bytes(ownership)
    if not ownership_path.exists() or ownership_path.read_bytes() != ownership_bytes:
        write_set[ownership_path] = ownership_bytes
        if ownership_path.exists():
            report["updated"].append(OWNERSHIP_FILE)
        else:
            report["created"].append(OWNERSHIP_FILE)

    # mkdir has no destructive collision semantics.  Refuse non-directory
    # nodes before making anything, then create the directory skeleton.
    for rel in directories:
        target = _contained_path(root, rel)
        if target is None:
            report["errors"].append(_issue("layout_target_escape", "directory escapes shared_root", path=rel))
        elif target.exists() and not target.is_dir():
            report["errors"].append(_issue("directory_collision", "required directory path is occupied", path=rel))
    if report["errors"]:
        report["created"] = []
        report["updated"] = []
        return report
    root.mkdir(parents=True, exist_ok=True)
    for rel in directories:
        assert _contained_path(root, rel) is not None
        (root / rel).mkdir(parents=True, exist_ok=True)

    ok, why, _ = _atomic_write_set(write_set)
    if not ok:
        report["errors"] = [_issue("layout_write_failed", f"atomic layout write failed: {why}")]
        report["created"] = []
        report["updated"] = []
        return report
    report["created"] = sorted(set(report["created"]))
    report["updated"] = sorted(set(report["updated"]))
    report["collisions"] = sorted(set(report["collisions"]))
    report["drifted"] = sorted(set(report["drifted"]))
    report["ok"] = True
    return report


def _parse_frontmatter(text: str) -> tuple[Optional[dict[str, str]], Optional[str]]:
    if not text.startswith("---\n"):
        return None, "missing"
    end = text.find("\n---", 4)
    if end < 0:
        return None, "unclosed"
    values: dict[str, str] = {}
    for line in text[4:end].splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line or line[:1].isspace():
            return None, f"invalid line: {line}"
        key, value = line.split(":", 1)
        key, value = key.strip(), value.strip()
        if not key or not value:
            return None, f"invalid scalar: {line}"
        values[key] = value.strip("\"'")
    return values, None


def _privacy_findings(text: str, rel: str) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for match in _SECRET_ASSIGN_RE.finditer(text):
        if not match.group(2).strip().startswith("[REDACTED"):
            findings.append(_issue(
                "secret_pattern", "credential-like assignment is forbidden",
                path=rel, severity="blocking",
            ))
            break
    patterns = (
        ("unscrubbable_secret", _PRIVATE_KEY_RE, "private-key material is forbidden"),
        ("secret_pattern", _BEARER_RE, "bearer credential is forbidden"),
        ("secret_pattern", _KNOWN_TOKEN_RE, "known credential token is forbidden"),
        ("secret_pattern", _CREDENTIAL_URL_RE, "credentials embedded in a URL are forbidden"),
        ("personal_email", _EMAIL_RE, "personal email address is forbidden"),
        ("personal_home_path", _HOME_RE, "machine-specific home path is forbidden"),
        ("personal_phone", _PHONE_RE, "phone-number-like personal data is forbidden"),
        ("transcript_content", _TRANSCRIPT_RE, "raw transcript-shaped content is forbidden"),
    )
    for code, pattern, message in patterns:
        if pattern.search(text):
            findings.append(_issue(code, message, path=rel, severity="blocking"))
    return findings


def _iter_tree(root: Path) -> Iterable[tuple[str, Path, Optional[Path]]]:
    """Yield lexical path, lexical node, and contained resolved node.

    Escaped symlinks are yielded with resolved=None and never traversed/read.
    """
    try:
        nodes = sorted(root.rglob("*"), key=lambda p: p.as_posix())
    except OSError:
        nodes = []
    for node in nodes:
        try:
            rel = node.relative_to(root).as_posix()
        except ValueError:
            continue
        yield rel, node, _contained_path(root, rel)


def lint_knowledge(start: Path | str, *, today: Optional[str] = None,
                   stale_days: int = 180) -> dict[str, Any]:
    ctx, errors = _workspace_context(start)
    report: dict[str, Any] = {
        "operation": "lint", "ok": False, "workspace_root": None,
        "shared_root": None, "blocking": [], "advisory": [], "errors": errors,
        "counts": {"documents": 0, "blocking": 0, "advisory": 0},
    }
    if ctx is None:
        return report
    root: Path = ctx["shared_root"]
    report["workspace_root"] = str(ctx["workspace_root"])
    report["shared_root"] = str(root)
    blocking: list[dict[str, Any]] = report["blocking"]
    advisory: list[dict[str, Any]] = report["advisory"]
    if not root.is_dir():
        blocking.append(_issue("shared_root_missing", "canonical shared_root does not exist", path=str(root), severity="blocking"))
        report["counts"]["blocking"] = 1
        return report

    ownership, ownership_error = _read_json(root / OWNERSHIP_FILE)
    if ownership_error or ownership is None or ownership.get("version") != SCHEMA_VERSION \
            or not isinstance(ownership.get("files"), dict):
        blocking.append(_issue("reserved_ownership_invalid", f"{OWNERSHIP_FILE} is missing or invalid: {ownership_error or 'wrong schema'}", path=OWNERSHIP_FILE, severity="blocking"))
        ownership = {"files": {}}
    index, index_error = _read_json(root / INDEX_FILE)
    indexed: set[str] = set()
    if index_error or index is None or index.get("version") != SCHEMA_VERSION \
            or not isinstance(index.get("documents"), list):
        blocking.append(_issue("reserved_index_invalid", f"{INDEX_FILE} is missing or invalid: {index_error or 'wrong schema'}", path=INDEX_FILE, severity="blocking"))
    else:
        for value in index["documents"]:
            rel = _relative_path(value) if isinstance(value, str) else None
            target = _contained_path(root, rel) if rel else None
            if rel is None or target is None:
                blocking.append(_issue("index_path_invalid", "index contains an unsafe document path", path=str(value), severity="blocking"))
            elif rel in indexed:
                blocking.append(_issue("index_duplicate", "index contains a duplicate document", path=rel, severity="blocking"))
            else:
                indexed.add(rel)
                if not target.is_file():
                    blocking.append(_issue("index_missing_document", "indexed document is missing", path=rel, severity="blocking"))

    owned_files = ownership.get("files", {}) if isinstance(ownership, dict) else {}
    for rel, expected in sorted(owned_files.items()):
        target = _contained_path(root, rel) if isinstance(rel, str) else None
        if target is None or not target.is_file() or target.is_symlink():
            blocking.append(_issue("owned_file_missing", "owned file is missing, unsafe, or non-regular", path=str(rel), severity="blocking"))
        else:
            try:
                actual = _sha_bytes(target.read_bytes())
            except OSError:
                actual = ""
            if not isinstance(expected, str) or actual != expected:
                blocking.append(_issue("owned_file_drift", "Asha-owned file differs from its recorded hash", path=rel, severity="blocking"))

    docs: dict[str, tuple[Path, dict[str, str]]] = {}
    for rel, lexical, resolved in _iter_tree(root):
        if resolved is None:
            blocking.append(_issue("document_escape", "path resolves outside canonical shared_root", path=rel, severity="blocking"))
            continue
        if lexical.is_symlink() and not resolved.exists():
            blocking.append(_issue("broken_symlink", "symlink target is unavailable", path=rel, severity="blocking"))
            continue
        if lexical.suffix.lower() != ".md" or not resolved.is_file():
            continue
        report["counts"]["documents"] += 1
        try:
            raw = resolved.read_bytes()
            text = raw.decode("utf-8")
        except (OSError, UnicodeDecodeError):
            blocking.append(_issue("document_unreadable", "document is unreadable or not strict UTF-8", path=rel, severity="blocking"))
            continue
        if not text.strip():
            advisory.append(_issue("empty_file", "empty canonical document", path=rel, severity="advisory"))
            continue
        frontmatter: dict[str, str] = {}
        if rel != "README.md":
            parsed, why = _parse_frontmatter(text)
            if why or parsed is None:
                blocking.append(_issue("malformed_frontmatter", f"frontmatter {why}", path=rel, severity="blocking"))
            else:
                frontmatter = parsed
                missing = [key for key in ("title", "type", "updated") if not parsed.get(key)]
                if missing:
                    blocking.append(_issue("frontmatter_schema", f"missing required fields: {', '.join(missing)}", path=rel, severity="blocking"))
        docs[rel] = (resolved, frontmatter)
        blocking.extend(_privacy_findings(text, rel))
        for link in _LINK_RE.findall(text):
            destination = link.strip().split()[0].strip("<>")
            if not destination or destination.startswith(("#", "http://", "https://", "mailto:", "data:")):
                continue
            destination = destination.split("#", 1)[0].split("?", 1)[0]
            try:
                candidate = (resolved.parent / destination).resolve()
                candidate.relative_to(root.resolve())
                exists = candidate.exists()
            except (OSError, RuntimeError, ValueError):
                exists = False
            if not exists:
                blocking.append(_issue("broken_link", f"relative link target is missing or outside shared_root: {destination}", path=rel, severity="blocking"))

    for rel in sorted(set(docs) - indexed - {"README.md"}):
        advisory.append(_issue("orphan_document", "document is not present in the canonical index", path=rel, severity="advisory"))

    for entry in ctx["manifest"].get("repositories", []):
        repo = entry.get("path")
        if not isinstance(repo, str):
            continue
        for filename in REPOSITORY_DOCUMENTS:
            rel = f"repos/{repo}/{filename}"
            target = _contained_path(root, rel)
            if target is None or not target.is_file():
                advisory.append(_issue("missing_coverage", "declared repository lacks a default knowledge document", path=rel, severity="advisory"))

    try:
        today_value = datetime.strptime(today, "%Y-%m-%d").date() if today else date.today()
    except ValueError:
        report["errors"] = [_issue("invalid_today", "today must be YYYY-MM-DD")]
        return report
    for rel, (_, fm) in docs.items():
        if not fm.get("updated"):
            continue
        try:
            stamp = datetime.strptime(fm["updated"], "%Y-%m-%d").date()
        except ValueError:
            blocking.append(_issue("frontmatter_date_invalid", "updated must be YYYY-MM-DD", path=rel, severity="blocking"))
            continue
        age = (today_value - stamp).days
        if age > stale_days:
            advisory.append(_issue("stale_document", f"document is {age} days old", path=rel, severity="advisory"))

    report["counts"]["blocking"] = len(blocking)
    report["counts"]["advisory"] = len(advisory)
    report["ok"] = not report["errors"] and not blocking
    return report


def _classify_source(ctx: dict[str, Any], source: Path,
                     explicit: Optional[str]) -> tuple[Optional[str], Optional[dict[str, Any]]]:
    allowed = {"personal", "operational", "canonical"}
    if explicit is not None and explicit not in allowed:
        return None, _issue("classification_invalid", "classification must be personal|operational|canonical")
    detected: Optional[str] = None
    ws_root: Path = ctx["workspace_root"]
    memory = ctx["manifest"].get("memory", {})
    roots = (
        ("operational", memory.get("operational_root", "Memory")),
        ("personal", memory.get("personal_root", "memory-local")),
        ("canonical", memory.get("shared_root", "knowledge")),
    )
    try:
        resolved_source = source.resolve()
        for label, rel in roots:
            plane = (ws_root / rel).resolve()
            if resolved_source == plane or plane in resolved_source.parents:
                detected = label
                break
    except (OSError, RuntimeError, ValueError):
        pass
    if detected and explicit and detected != explicit:
        return None, _issue("classification_conflict", f"source is in the {detected} plane, not {explicit}")
    if detected:
        return detected, None
    if explicit:
        return explicit, None
    return None, _issue("classification_required", "source is outside configured planes; provide an explicit classification")


def _strip_frontmatter(text: str) -> str:
    if not text.startswith("---\n"):
        return text
    end = text.find("\n---", 4)
    if end < 0:
        return text
    return text[end + 4:].lstrip("\r\n")


def scrub_candidate(text: str) -> tuple[Optional[str], list[dict[str, Any]], list[dict[str, Any]]]:
    """Deterministically scrub portable text; reject what cannot be made safe."""
    if _PRIVATE_KEY_RE.search(text):
        return None, [], [_issue("unscrubbable_secret", "private-key material cannot be safely promoted")]
    if _TRANSCRIPT_RE.search(text):
        return None, [], [_issue("transcript_content_forbidden", "raw transcript-shaped content cannot be promoted")]
    text = _strip_frontmatter(text)
    scrubbed: list[dict[str, Any]] = []

    def replace(pattern: re.Pattern[str], replacement: str, code: str) -> None:
        nonlocal text
        text, count = pattern.subn(replacement, text)
        if count:
            scrubbed.append(_issue(code, f"{count} occurrence(s) scrubbed"))

    transient: list[str] = []
    kept: list[str] = []
    for line in text.splitlines():
        if _TRANSIENT_LINE_RE.search(line):
            transient.append(line)
        else:
            kept.append(line)
    if transient:
        scrubbed.append(_issue("transient_line_removed", f"{len(transient)} transient line(s) removed"))
    text = "\n".join(kept)
    if text and not text.endswith("\n"):
        text += "\n"

    def assignment(match: re.Match[str]) -> str:
        return match.group(1) + "[REDACTED]"

    text, count = _SECRET_ASSIGN_RE.subn(assignment, text)
    if count:
        scrubbed.append(_issue("credential_redacted", f"{count} credential assignment(s) redacted"))
    replace(_BEARER_RE, "Bearer [REDACTED]", "credential_redacted")
    replace(_KNOWN_TOKEN_RE, "[REDACTED_TOKEN]", "credential_redacted")
    replace(_CREDENTIAL_URL_RE, r"\1[REDACTED]@", "credential_url_redacted")
    replace(_EMAIL_RE, "[REDACTED_EMAIL]", "personal_email_redacted")
    replace(_HOME_RE, "~", "home_path_normalized")
    replace(_PHONE_RE, "[REDACTED_PHONE]", "personal_phone_redacted")

    residual = _privacy_findings(text, "candidate")
    if residual:
        return None, scrubbed, [_issue("scrub_incomplete", "candidate still contains blocked private or secret material")]
    if not text.strip():
        return None, scrubbed, [_issue("candidate_empty", "candidate is empty after scrubbing")]
    return text, scrubbed, []


def _promotion_target(ctx: dict[str, Any], target: str) -> tuple[Optional[Path], Optional[str], Optional[dict[str, Any]]]:
    rel = _relative_path(target)
    if rel is None or not rel.endswith(".md"):
        return None, None, _issue("target_invalid", "target must be a safe shared-root-relative Markdown path")
    parts = PurePosixPath(rel).parts
    if not parts or parts[0] not in ALLOWED_TARGET_ROOTS:
        return None, None, _issue("target_reserved", f"target must live under one of {sorted(ALLOWED_TARGET_ROOTS)}")
    if parts[0] == "repos":
        declared = {entry.get("path") for entry in ctx["manifest"].get("repositories", [])}
        if len(parts) < 3 or parts[1] not in declared:
            return None, None, _issue("target_repository_invalid", "repository target must name a declared repository")
    shared_lexical = _lexical_contained_path(ctx["workspace_root"], ctx["shared_rel"])
    path = _lexical_contained_path(shared_lexical, rel) if shared_lexical is not None else None
    if shared_lexical is None or path is None:
        return None, None, _issue(
            "target_unsafe",
            "promotion target path escapes shared_root or traverses a symlink",
            path=rel,
        )
    return path, rel, None


def _evidence_records(ctx: dict[str, Any], evidence: Iterable[Path | str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    refs = list(evidence)
    if not refs:
        return [], [_issue("evidence_required", "promotion requires at least one source-code, configuration, test, or stated evidence reference")]
    records: list[dict[str, Any]] = []
    ws_root: Path = ctx["workspace_root"]
    for value in refs:
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = ws_root / candidate
        try:
            resolved = candidate.resolve()
            rel = resolved.relative_to(ws_root).as_posix()
        except (OSError, RuntimeError, ValueError):
            return [], [_issue("evidence_escape", "evidence must resolve inside the workspace", path=str(value))]
        if not resolved.is_file() or resolved.is_symlink():
            return [], [_issue("evidence_unavailable", "evidence must be an available regular file", path=rel)]
        try:
            digest = _sha_bytes(resolved.read_bytes())
        except OSError as exc:
            return [], [_issue("evidence_unreadable", str(exc), path=rel)]
        if rel.startswith("tests/") or "/tests/" in f"/{rel}/":
            kind = "test"
        elif resolved.suffix.lower() in {".json", ".yaml", ".yml", ".toml", ".ini"}:
            kind = "configuration"
        elif rel.startswith(ctx["shared_rel"] + "/"):
            kind = "canonical"
        else:
            kind = "source"
        records.append({"path": rel, "sha256": digest, "kind": kind, "verified": "exists-and-hashed"})
    return sorted(records, key=lambda item: item["path"]), []


def _canonical_content(rel: str, classification: str, body: str,
                       evidence: list[dict[str, Any]]) -> str:
    parts = PurePosixPath(rel).parts
    if parts[0] == "repos":
        doc_type = "repository-context"
    elif parts[0] == "decisions":
        doc_type = "decision"
    elif parts[0] == "tickets":
        doc_type = "work-item"
    else:
        doc_type = "knowledge"
    title = _title_from_name(rel)
    evidence_value = ", ".join(item["path"] for item in evidence)
    lines = [
        "---", f"title: {title}", f"type: {doc_type}", "status: canonical",
        f"updated: {date.today().isoformat()}",
        f"source_classification: {classification}", f"evidence: {evidence_value}",
        "---", "", f"# {title}", "", "## Canonical knowledge", "", body.rstrip(), "",
    ]
    return "\n".join(lines)


def _plan_digest(plan: dict[str, Any]) -> str:
    material = {key: value for key, value in plan.items() if key not in {"plan_digest", "ok"}}
    return _sha_bytes(json.dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def _normalize_github_remote(value: str) -> Optional[dict[str, str]]:
    """Return a credential-free GitHub repository binding."""
    raw = value.strip()
    match = re.fullmatch(r"git@github\.com:([^/\s]+)/([^\s]+)", raw, re.I)
    if match:
        owner, repo = match.groups()
    else:
        try:
            parsed = urlsplit(raw)
            port = parsed.port
        except ValueError:
            return None
        scheme = parsed.scheme.lower()
        if (parsed.hostname or "").lower() != "github.com" or parsed.query or parsed.fragment \
                or port is not None or scheme not in {"https", "ssh"}:
            return None
        if scheme == "https" and (parsed.username is not None or parsed.password is not None):
            return None
        if scheme == "ssh" and (parsed.username not in {None, "git"} or parsed.password is not None):
            return None
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) != 2:
            return None
        owner, repo = parts
    repo = re.sub(r"\.git$", "", repo, flags=re.I)
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", owner) or not re.fullmatch(r"[A-Za-z0-9_.-]+", repo):
        return None
    identity = f"{owner}/{repo}"
    return {"forge": "github", "repository": identity,
            "remote_url": f"https://github.com/{identity}"}


def _plan_publication_binding(ctx: dict[str, Any]) -> tuple[Optional[dict[str, str]], Optional[dict[str, Any]]]:
    workspace: Path = ctx["workspace_root"]
    shared_git_rel = ctx["manifest"].get("memory", {}).get("shared_git_root", ".")
    try:
        git_root = (workspace / shared_git_rel).resolve()
        git_root.relative_to(workspace)
        ctx["shared_root"].resolve().relative_to(git_root)
    except (OSError, RuntimeError, ValueError):
        return None, _issue(
            "shared_root_git_escape",
            "canonical shared_root must be contained by shared_git_root for pull-request promotion",
        )

    def read(args: list[str]) -> subprocess.CompletedProcess[str]:
        return _run_publication_command(args, cwd=git_root)

    top = read(["git", "rev-parse", "--show-toplevel"])
    if top.returncode != 0:
        return None, _issue("shared_git_root_unavailable", "shared_git_root is not an available Git worktree")
    try:
        actual_top = Path(top.stdout.strip()).resolve()
    except (OSError, RuntimeError):
        actual_top = Path()
    if actual_top != git_root:
        return None, _issue("shared_git_root_mismatch", "Git top-level does not equal configured shared_git_root")
    branch = read(["git", "symbolic-ref", "--quiet", "--short", "HEAD"])
    base_branch = branch.stdout.strip()
    if branch.returncode != 0 or not base_branch or base_branch.startswith("-"):
        return None, _issue("base_branch_ambiguous", "shared Git root must be on a named branch during review")
    oid = read(["git", "rev-parse", "HEAD"])
    base_oid = oid.stdout.strip()
    if oid.returncode != 0 or not re.fullmatch(r"[0-9a-fA-F]{40,64}", base_oid):
        return None, _issue("base_commit_unavailable", "shared Git root must have a resolvable base commit")
    remote = read(["git", "remote", "get-url", "origin"])
    normalized = _normalize_github_remote(remote.stdout) if remote.returncode == 0 else None
    if normalized is None:
        return None, _issue(
            "review_remote_unavailable",
            "pull-request promotion requires a credential-free GitHub origin remote",
        )
    return {
        "git_root": shared_git_rel, "base_branch": base_branch,
        "base_oid": base_oid.lower(), "remote_name": "origin", **normalized,
    }, None


def plan_promotion(*, start: Path | str, source: Path | str, target: str,
                   evidence: Iterable[Path | str], classification: Optional[str] = None,
                   requested_mode: Optional[str] = None) -> dict[str, Any]:
    """Prepare a reviewable promotion write-set.  No files are changed."""
    ctx, errors = _workspace_context(start)
    report: dict[str, Any] = {
        "version": SCHEMA_VERSION, "operation": "promote-plan", "ok": False,
        "errors": errors, "source": str(source), "target": target,
    }
    if ctx is None:
        return report
    root: Path = ctx["shared_root"]
    if not root.is_dir():
        report["errors"] = [_issue("layout_required", "initialize the canonical knowledge layout before promotion")]
        return report
    source_path = Path(source)
    if _TRANSCRIPT_PATH_RE.search(source_path.name):
        report["errors"] = [_issue("transcript_source_forbidden", "raw transcript/session-log sources cannot be promoted")]
        return report
    if not source_path.is_absolute():
        source_path = ctx["workspace_root"] / source_path
    if not source_path.is_file() or source_path.is_symlink():
        report["errors"] = [_issue("source_unavailable", "promotion source must be an available regular file", path=str(source_path))]
        return report
    source_class, class_error = _classify_source(ctx, source_path, classification)
    if class_error:
        report["errors"] = [class_error]
        return report
    target_path, target_rel, target_error = _promotion_target(ctx, target)
    if target_error:
        report["errors"] = [target_error]
        return report
    lint = lint_knowledge(start)
    if not lint["ok"]:
        report["errors"] = [_issue("knowledge_lint_failed", "existing canonical knowledge has blocking lint findings")]
        report["lint"] = lint
        return report
    evidence_records, evidence_errors = _evidence_records(ctx, evidence)
    if evidence_errors:
        report["errors"] = evidence_errors
        return report
    try:
        raw = source_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        report["errors"] = [_issue("source_unreadable", f"source must be strict UTF-8: {exc}")]
        return report
    scrubbed_text, scrubbed, scrub_errors = scrub_candidate(raw)
    if scrub_errors or scrubbed_text is None:
        report["errors"] = scrub_errors
        report["scrubbed"] = scrubbed
        return report

    configured_mode = ctx["manifest"].get("memory", {}).get("promotion_mode", "pull-request")
    mode = requested_mode or configured_mode
    if mode not in {"pull-request", "direct-commit"}:
        report["errors"] = [_issue("promotion_mode_invalid", "mode must be pull-request|direct-commit")]
        return report
    if mode == "direct-commit" and configured_mode != "direct-commit":
        report["errors"] = [_issue("direct_commit_not_configured", "direct-commit is allowed only when the workspace manifest explicitly configures it")]
        return report
    assert target_path is not None and target_rel is not None and source_class is not None
    if target_path.exists() and (not target_path.is_file() or target_path.is_symlink()):
        report["errors"] = [_issue("target_unsafe", "promotion target must be a non-symlink regular file", path=target_rel)]
        return report
    try:
        source_sha = _sha_bytes(source_path.read_bytes())
        target_preimage = (
            {"state": "present", "sha256": _sha_bytes(target_path.read_bytes())}
            if target_path.exists() else {"state": "absent", "sha256": None}
        )
    except OSError as exc:
        report["errors"] = [_issue("preimage_unreadable", str(exc))]
        return report
    content = _canonical_content(target_rel, source_class, scrubbed_text, evidence_records)
    publication = None
    if mode == "pull-request":
        publication, publication_error = _plan_publication_binding(ctx)
        if publication_error:
            report["errors"] = [publication_error]
            return report
    next_steps = ["validate_plan"]
    if mode == "pull-request":
        next_steps.append("create_dedicated_branch")
    next_steps.extend(["apply_write_set", "run_workspace_lint", "commit_shared_root"])
    if mode == "pull-request":
        next_steps.append("open_pull_request")
    report.update({
        "ok": True,
        "errors": [],
        "workspace_root": str(ctx["workspace_root"]),
        "shared_root": str(root),
        "target_path": str(target_path),
        "target_canonical_path": str(target_path.resolve()),
        "target": target_rel,
        "source_preimage": {"path": str(source_path), "sha256": source_sha},
        "target_preimage": target_preimage,
        "classification": source_class,
        "promotion_mode": mode,
        "review_required": mode == "pull-request",
        "evidence": evidence_records,
        "scrubbed": scrubbed,
        "content": content,
        "publication": publication,
        "next_steps": next_steps,
        "governance": {
            "git_operations_executed": False,
            "push_or_merge_allowed": False,
            "review_adapter_required": mode == "pull-request",
            "scrub_limitations": "deterministic pattern checks do not establish semantic truth; evidence still requires human review",
        },
    })
    report["plan_digest"] = _plan_digest(report)
    return report


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _secure_json_write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(_json_bytes(value))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        _fsync_dir(path.parent)
    finally:
        tmp.unlink(missing_ok=True)


def write_promotion_plan(plan: dict[str, Any], plan_out: Path | str) -> dict[str, Any]:
    """Persist an immutable review artifact inside the workspace."""
    result = {"operation": "promote-plan", "ok": False, "errors": []}
    if not isinstance(plan, dict) or not plan.get("ok") or plan.get("version") != SCHEMA_VERSION:
        result["errors"] = [_issue("plan_invalid", "only a successful supported plan can be persisted")]
        return result
    if plan.get("plan_digest") != _plan_digest(plan):
        result["errors"] = [_issue("plan_tampered", "promotion plan no longer matches its digest")]
        return result
    workspace_root = Path(plan["workspace_root"])
    candidate = Path(plan_out)
    if not candidate.is_absolute():
        candidate = workspace_root / candidate
    try:
        rel = candidate.absolute().relative_to(workspace_root.absolute()).as_posix()
    except ValueError:
        rel = ""
    destination = _lexical_contained_path(workspace_root, rel) if rel else None
    if destination is None:
        result["errors"] = [_issue("plan_path_unsafe", "plan artifact must remain inside the workspace without symlink aliases")]
        return result
    if destination.exists():
        result["errors"] = [_issue("plan_exists", "review artifacts are immutable; choose a new --plan-out", path=rel)]
        return result
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Recheck after mkdir so a raced parent alias cannot be accepted.
        if _lexical_contained_path(workspace_root, rel) != destination:
            raise OSError("plan path changed during creation")
        _secure_json_write(destination, plan)
    except OSError as exc:
        result["errors"] = [_issue("plan_write_failed", str(exc), path=rel)]
        return result
    result.update(plan)
    result.update({"operation": "promote-plan", "ok": True, "plan_path": str(destination)})
    return result


def _verify_plan_preimages(plan: dict[str, Any], ctx: dict[str, Any], target: Path) -> Optional[dict[str, Any]]:
    source_info = plan.get("source_preimage")
    if not isinstance(source_info, dict) or not isinstance(source_info.get("path"), str):
        return _issue("plan_invalid", "plan lacks a bound source preimage")
    source = Path(source_info["path"])
    if source.is_symlink() or not source.is_file():
        return _issue("source_changed", "planned source is missing or unsafe", path=str(source))
    try:
        if _sha_bytes(source.read_bytes()) != source_info.get("sha256"):
            return _issue("source_changed", "planned source changed after review", path=str(source))
    except OSError:
        return _issue("source_changed", "planned source is unreadable", path=str(source))

    for evidence in plan.get("evidence", []):
        rel = evidence.get("path") if isinstance(evidence, dict) else None
        candidate = _lexical_contained_path(ctx["workspace_root"], rel) if isinstance(rel, str) else None
        if candidate is None or candidate.is_symlink() or not candidate.is_file():
            return _issue("evidence_changed", "planned evidence is missing or unsafe", path=str(rel))
        try:
            current_sha = _sha_bytes(candidate.read_bytes())
        except OSError:
            current_sha = ""
        if current_sha != evidence.get("sha256"):
            return _issue("evidence_changed", "planned evidence changed after review", path=rel)

    expected = plan.get("target_preimage")
    if not isinstance(expected, dict) or expected.get("state") not in {"absent", "present"}:
        return _issue("plan_invalid", "plan lacks a bound target preimage")
    if target.is_symlink() or (target.exists() and not target.is_file()):
        return _issue("target_changed", "promotion target became unsafe", path=plan.get("target"))
    if expected["state"] == "absent":
        if target.exists():
            return _issue("target_changed", "promotion target was created after review", path=plan.get("target"))
    elif not target.is_file():
        return _issue("target_changed", "promotion target disappeared after review", path=plan.get("target"))
    else:
        try:
            current = _sha_bytes(target.read_bytes())
        except OSError:
            current = ""
        if current != expected.get("sha256"):
            return _issue("target_changed", "promotion target changed after review", path=plan.get("target"))
    return None


def _promotion_paths(plan: dict[str, Any]) -> tuple[Optional[dict[str, Path]], Optional[dict[str, Any]]]:
    ctx, errors = _workspace_context(plan.get("workspace_root", ""))
    if ctx is None:
        return None, errors[0]
    if Path(plan.get("shared_root", "")) != ctx["shared_root"]:
        return None, _issue("shared_root_changed", "workspace shared_root changed after review")
    target, rel, error = _promotion_target(ctx, plan.get("target", ""))
    if error or target is None or rel is None or str(target) != plan.get("target_path"):
        return None, _issue("target_unsafe", "promotion target path changed or traverses a symlink")
    shared_lexical = _lexical_contained_path(ctx["workspace_root"], ctx["shared_rel"])
    index = _lexical_contained_path(shared_lexical, INDEX_FILE) if shared_lexical is not None else None
    ownership = _lexical_contained_path(shared_lexical, OWNERSHIP_FILE) if shared_lexical is not None else None
    if index is None or ownership is None:
        return None, _issue("reserved_path_unsafe", "index or ownership metadata traverses a symlink")
    if str(target.resolve()) != plan.get("target_canonical_path"):
        return None, _issue("target_unsafe", "promotion target canonical path changed after review")
    return {"workspace": ctx["workspace_root"], "root": ctx["shared_root"],
            "target": target, "index": index, "ownership": ownership}, None


def _build_promotion_write_set(plan: dict[str, Any]) -> tuple[Optional[dict[Path, bytes]], Optional[dict[str, Any]]]:
    paths, error = _promotion_paths(plan)
    if error or paths is None:
        return None, error
    index, index_error = _read_json(paths["index"])
    ownership, ownership_error = _read_json(paths["ownership"])
    if index_error or index is None or not isinstance(index.get("documents"), list):
        return None, _issue("reserved_index_invalid", f"cannot update index: {index_error or 'wrong schema'}")
    if ownership_error or ownership is None or not isinstance(ownership.get("files"), dict):
        return None, _issue("ownership_invalid", f"cannot update ownership metadata: {ownership_error or 'wrong schema'}")
    index["documents"] = sorted(set(str(item) for item in index["documents"]) | {plan["target"]})
    index_bytes = _json_bytes(index)
    if INDEX_FILE in ownership["files"]:
        ownership["files"][INDEX_FILE] = _sha_bytes(index_bytes)
    return {
        paths["target"]: plan["content"].encode("utf-8"),
        paths["index"]: index_bytes,
        paths["ownership"]: _json_bytes(ownership),
    }, None


def _journal_path(plan: dict[str, Any]) -> Optional[Path]:
    root = Path(plan.get("workspace_root", ""))
    return _lexical_contained_path(root, f".asha/state/knowledge-transactions/{plan.get('plan_digest', '')}.json")


def _prepare_promotion_transaction(plan: dict[str, Any], write_set: Optional[dict[Path, bytes]] = None) -> dict[str, Any]:
    """Durably record originals before any member of the write-set changes."""
    if write_set is None:
        write_set, error = _build_promotion_write_set(plan)
        if error or write_set is None:
            return {"ok": False, "errors": [error]}
    journal = _journal_path(plan)
    if journal is None:
        return {"ok": False, "errors": [_issue("journal_path_unsafe", "transaction journal path is unsafe")]}
    if journal.exists() or journal.is_symlink():
        return {"ok": False, "errors": [_issue("journal_exists", "an unfinished promotion transaction requires recovery")]}
    workspace = Path(plan["workspace_root"])
    entries = []
    try:
        for path, desired in sorted(write_set.items(), key=lambda item: str(item[0])):
            rel = path.absolute().relative_to(workspace.absolute()).as_posix()
            safe = _lexical_contained_path(workspace, rel)
            if safe != path:
                raise OSError(f"unsafe transaction member: {rel}")
            original = path.read_bytes() if path.exists() else None
            entries.append({
                "path": rel,
                "original": None if original is None else base64.b64encode(original).decode("ascii"),
                "original_sha256": None if original is None else _sha_bytes(original),
                "desired_sha256": _sha_bytes(desired),
            })
        payload = {"version": SCHEMA_VERSION, "workspace_root": str(workspace),
                   "plan_digest": plan["plan_digest"], "state": "prepared", "entries": entries}
        _secure_json_write(journal, payload)
    except (OSError, ValueError) as exc:
        return {"ok": False, "errors": [_issue("journal_write_failed", str(exc))]}
    return {
        "ok": True, "journal_path": str(journal), "errors": [],
        "preimages": {entry["path"]: entry["original_sha256"] for entry in entries},
    }


def recover_promotion_journal(journal_path: Path | str) -> dict[str, Any]:
    """Recover a prepared promotion using original/desired hash discrimination."""
    result = {"operation": "promote-recover", "ok": False, "errors": []}
    journal = Path(journal_path)
    if journal.is_symlink() or not journal.is_file():
        result["errors"] = [_issue("journal_invalid", "transaction journal is missing or unsafe")]
        return result
    data, why = _read_json(journal)
    if why or data is None or data.get("state") != "prepared" or not isinstance(data.get("entries"), list):
        result["errors"] = [_issue("journal_invalid", why or "wrong journal schema")]
        return result
    workspace = Path(data.get("workspace_root", ""))
    expected = _lexical_contained_path(workspace, f".asha/state/knowledge-transactions/{data.get('plan_digest', '')}.json")
    if expected != journal:
        result["errors"] = [_issue("journal_invalid", "journal location does not match its workspace and digest")]
        return result
    checked: list[tuple[Path, dict[str, Any], str | None]] = []
    conflicts: list[dict[str, Any]] = []
    for entry in data["entries"]:
        path = _lexical_contained_path(workspace, entry.get("path", "")) if isinstance(entry, dict) else None
        if path is None:
            conflicts.append(_issue("recovery_conflict", "transaction member is unsafe", path=entry.get("path") if isinstance(entry, dict) else None))
            continue
        try:
            current = _sha_bytes(path.read_bytes()) if path.exists() else None
        except OSError:
            current = "unreadable"
        if current not in {entry.get("original_sha256"), entry.get("desired_sha256")}:
            conflicts.append(_issue("recovery_conflict", "transaction member has an unrecognized state", path=entry["path"]))
            continue
        checked.append((path, entry, current))
    try:
        for path, entry, current in checked:
            if current != entry.get("desired_sha256"):
                continue
            encoded = entry.get("original")
            if encoded is None:
                _unlink_contained(
                    workspace, path, expected_sha=entry.get("desired_sha256"),
                )
            else:
                original = base64.b64decode(encoded, validate=True)
                if _sha_bytes(original) != entry.get("original_sha256"):
                    raise ValueError("journal original hash mismatch")
                _replace_contained(
                    workspace, path, original,
                    expected_sha=entry.get("desired_sha256"),
                )
        if not conflicts:
            journal.unlink()
            _fsync_dir(journal.parent)
    except (OSError, ValueError) as exc:
        result["errors"] = [_issue("recovery_failed", str(exc))]
        return result
    if conflicts:
        result["errors"] = conflicts
        result["recovered_safe_members"] = True
        return result
    result["ok"] = True
    return result


def apply_promotion_artifact(plan_path: Path | str, *, confirmed_digest: str,
                             confirmed: bool = False) -> dict[str, Any]:
    """Apply only a durable reviewed artifact with explicit digest confirmation."""
    result = {"operation": "promote-apply", "ok": False, "changed": [], "errors": []}
    if not confirmed:
        result["errors"] = [_issue("confirmation_required", "promotion apply requires explicit confirmation")]
        return result
    artifact = Path(plan_path)
    if artifact.is_symlink() or not artifact.is_file():
        result["errors"] = [_issue("plan_invalid", "reviewed plan artifact is missing or unsafe")]
        return result
    plan, why = _read_json(artifact)
    if why or plan is None or not plan.get("ok") or plan.get("version") != SCHEMA_VERSION:
        result["errors"] = [_issue("plan_invalid", why or "reviewed plan has an unsupported schema")]
        return result
    if not re.fullmatch(r"[0-9a-f]{64}", confirmed_digest or "") \
            or confirmed_digest != plan.get("plan_digest"):
        result["errors"] = [_issue("digest_confirmation_mismatch", "--digest must exactly match the reviewed artifact")]
        return result
    if plan.get("plan_digest") != _plan_digest(plan):
        result["errors"] = [_issue("plan_tampered", "promotion plan no longer matches its digest")]
        return result
    workspace = Path(plan["workspace_root"])
    try:
        artifact_rel = artifact.absolute().relative_to(workspace.absolute()).as_posix()
    except ValueError:
        artifact_rel = ""
    if not artifact_rel or _lexical_contained_path(workspace, artifact_rel) != artifact:
        result["errors"] = [_issue("plan_invalid", "review artifact escaped the planned workspace or traverses a symlink")]
        return result
    paths, error = _promotion_paths(plan)
    if error or paths is None:
        result["errors"] = [error]
        return result
    ctx, _ = _workspace_context(workspace)
    assert ctx is not None
    configured_mode = ctx["manifest"].get("memory", {}).get("promotion_mode", "pull-request")
    if plan.get("promotion_mode") == "direct-commit" and configured_mode != "direct-commit":
        result["errors"] = [_issue("direct_commit_not_configured", "current manifest no longer permits direct-commit")]
        return result
    pending = _journal_path(plan)
    if pending is None:
        result["errors"] = [_issue("journal_path_unsafe", "transaction journal path is unsafe")]
        return result
    if pending.exists() or pending.is_symlink():
        recovery = recover_promotion_journal(pending)
        if not recovery["ok"]:
            result["errors"] = [_issue("recovery_required", "unfinished promotion could not be recovered")]
            result["recovery"] = recovery
            return result
    error = _verify_plan_preimages(plan, ctx, paths["target"])
    if error:
        result["errors"] = [error]
        return result
    write_set, error = _build_promotion_write_set(plan)
    if error or write_set is None:
        result["errors"] = [error]
        return result
    # Last check before journaling/writing closes the review-to-apply race window.
    error = _verify_plan_preimages(plan, ctx, paths["target"])
    if error:
        result["errors"] = [error]
        return result
    prepared = _prepare_promotion_transaction(plan, write_set)
    if not prepared["ok"]:
        result["errors"] = prepared["errors"]
        return result
    journal = Path(prepared["journal_path"])
    paths_after_journal, path_error = _promotion_paths(plan)
    preimage_error = (
        path_error if path_error else
        _verify_plan_preimages(plan, ctx, paths_after_journal["target"])
    )
    if preimage_error:
        recovery = recover_promotion_journal(journal)
        result["errors"] = [preimage_error]
        result["recovery"] = recovery
        return result
    try:
        for destination, content in sorted(write_set.items(), key=lambda item: str(item[0])):
            rel = destination.absolute().relative_to(workspace.absolute()).as_posix()
            _replace_contained(
                workspace, destination, content,
                expected_sha=prepared["preimages"][rel],
            )
        lint = lint_knowledge(workspace)
        if not lint["ok"]:
            raise RuntimeError("post-write lint failed")
    except (OSError, RuntimeError) as exc:
        recovery = recover_promotion_journal(journal)
        result["errors"] = [_issue("promotion_write_failed", f"promotion failed and recovery was attempted: {exc}")]
        result["recovery"] = recovery
        return result
    journal.unlink()
    _fsync_dir(journal.parent)
    result.update({
        "ok": True,
        "changed": sorted([plan["target"], INDEX_FILE, OWNERSHIP_FILE]),
        "lint": lint,
        "promotion_mode": plan["promotion_mode"],
        "review_required": plan["review_required"],
        "next_steps": [step for step in plan["next_steps"] if step not in {"validate_plan", "apply_write_set", "run_workspace_lint"}],
    })
    return result


def _run_publication_command(args: list[str], *, cwd: Path,
                             input_text: Optional[str] = None) -> subprocess.CompletedProcess[str]:
    """Run one argv-only publication command without invoking a shell."""
    try:
        return subprocess.run(
            args, cwd=cwd, input=input_text, text=True,
            capture_output=True, check=False,
        )
    except OSError as exc:
        return subprocess.CompletedProcess(args, 127, "", str(exc))


def publish_promotion_artifact(plan_path: Path | str, *, confirmed_digest: str,
                               confirmed: bool = False, run_git_hooks: bool = False,
                               runner=None) -> dict[str, Any]:
    """Publish a reviewed pull-request plan without ever merging it."""
    result: dict[str, Any] = {
        "operation": "promote-publish", "ok": False, "errors": [],
        "git_operations_executed": False, "merge_executed": False,
        "git_hooks_executed": run_git_hooks,
    }
    if not confirmed:
        result["errors"] = [_issue(
            "confirmation_required", "promotion publish requires explicit confirmation",
        )]
        return result
    artifact = Path(plan_path)
    if artifact.is_symlink() or not artifact.is_file():
        result["errors"] = [_issue("plan_invalid", "reviewed plan artifact is missing or unsafe")]
        return result
    plan, why = _read_json(artifact)
    if why or plan is None or not plan.get("ok") or plan.get("version") != SCHEMA_VERSION:
        result["errors"] = [_issue("plan_invalid", why or "reviewed plan has an unsupported schema")]
        return result
    if not re.fullmatch(r"[0-9a-f]{64}", confirmed_digest or "") \
            or confirmed_digest != plan.get("plan_digest"):
        result["errors"] = [_issue("digest_confirmation_mismatch", "--digest must exactly match the reviewed artifact")]
        return result
    if plan.get("plan_digest") != _plan_digest(plan):
        result["errors"] = [_issue("plan_tampered", "promotion plan no longer matches its digest")]
        return result
    if plan.get("promotion_mode") != "pull-request" or not plan.get("review_required"):
        result["errors"] = [_issue(
            "pull_request_plan_required",
            "publish accepts only a reviewed plan configured for pull-request promotion",
        )]
        return result

    workspace = Path(plan.get("workspace_root", ""))
    try:
        artifact_rel = artifact.absolute().relative_to(workspace.absolute()).as_posix()
    except ValueError:
        artifact_rel = ""
    if not artifact_rel or _lexical_contained_path(workspace, artifact_rel) != artifact:
        result["errors"] = [_issue("plan_invalid", "review artifact escaped the planned workspace or traverses a symlink")]
        return result
    ctx, errors = _workspace_context(workspace)
    if ctx is None:
        result["errors"] = errors
        return result
    configured_mode = ctx["manifest"].get("memory", {}).get("promotion_mode", "pull-request")
    if configured_mode != "pull-request":
        result["errors"] = [_issue("promotion_mode_changed", "workspace no longer permits pull-request promotion")]
        return result
    paths, path_error = _promotion_paths(plan)
    if path_error or paths is None:
        result["errors"] = [path_error]
        return result
    preimage_error = _verify_plan_preimages(plan, ctx, paths["target"])
    if preimage_error:
        result["errors"] = [preimage_error]
        return result
    write_set, write_error = _build_promotion_write_set(plan)
    if write_error or write_set is None:
        result["errors"] = [write_error]
        return result

    publication = plan.get("publication")
    if not isinstance(publication, dict):
        result["errors"] = [_issue("plan_invalid", "pull-request plan lacks a bound publication destination")]
        return result
    shared_git_rel = ctx["manifest"].get("memory", {}).get("shared_git_root", ".")
    if publication.get("git_root") != shared_git_rel:
        result["errors"] = [_issue("shared_git_root_changed", "shared_git_root changed after review")]
        return result
    try:
        git_root = (workspace / shared_git_rel).resolve()
        git_root.relative_to(workspace.resolve())
    except (OSError, RuntimeError, ValueError):
        result["errors"] = [_issue("shared_git_root_escape", "configured shared_git_root resolves outside the workspace")]
        return result
    try:
        staged_paths = sorted(
            path.resolve().relative_to(git_root).as_posix() for path in write_set
        )
    except ValueError:
        result["errors"] = [_issue(
            "write_set_git_escape",
            "reviewed write-set is outside the configured shared_git_root",
        )]
        return result
    run = runner or _run_publication_command

    def command(args: list[str], *, input_text: Optional[str] = None) -> subprocess.CompletedProcess[str]:
        return run(args, cwd=git_root, input_text=input_text)

    top = command(["git", "rev-parse", "--show-toplevel"])
    if top.returncode != 0:
        result["errors"] = [_issue("shared_git_root_unavailable", "configured shared_git_root is not an available Git worktree")]
        return result
    try:
        actual_top = Path(top.stdout.strip()).resolve()
    except (OSError, RuntimeError):
        actual_top = Path()
    if actual_top != git_root:
        result["errors"] = [_issue("shared_git_root_mismatch", "Git top-level does not equal configured shared_git_root")]
        return result
    current = command(["git", "symbolic-ref", "--quiet", "--short", "HEAD"])
    base = current.stdout.strip()
    if current.returncode != 0 or base != publication.get("base_branch"):
        result["errors"] = [_issue("base_branch_changed", "reviewed base branch is not currently checked out")]
        return result
    oid = command(["git", "rev-parse", "HEAD"])
    if oid.returncode != 0 or oid.stdout.strip().lower() != publication.get("base_oid"):
        result["errors"] = [_issue("base_commit_changed", "reviewed base commit changed before publication")]
        return result
    dirty = command(["git", "status", "--porcelain", "--untracked-files=all"])
    if dirty.returncode != 0:
        result["errors"] = [_issue("shared_git_status_failed", "could not inspect shared Git worktree status")]
        return result
    if dirty.stdout:
        result["errors"] = [_issue("shared_git_root_dirty", "shared Git worktree must be clean before publication")]
        return result
    remote = command(["git", "remote", "get-url", "origin"])
    normalized_remote = _normalize_github_remote(remote.stdout) if remote.returncode == 0 else None
    if normalized_remote is None or any(
        normalized_remote.get(key) != publication.get(key)
        for key in ("forge", "repository", "remote_url")
    ):
        result["errors"] = [_issue("review_remote_changed", "reviewed GitHub origin changed before publication")]
        return result

    branch = f"asha/knowledge-{confirmed_digest[:12]}"
    exists = command(["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"])
    if exists.returncode == 0:
        result["errors"] = [_issue("publication_branch_exists", "dedicated promotion branch already exists", path=branch)]
        return result
    if exists.returncode != 1:
        result["errors"] = [_issue("publication_branch_check_failed", "could not verify the dedicated branch name")]
        return result

    switched = command(["git", "switch", "-c", branch])
    if switched.returncode != 0:
        result["errors"] = [_issue("publication_branch_failed", "could not create the dedicated promotion branch")]
        return result
    result.update({"git_operations_executed": True, "branch": branch, "base": base})

    applied = apply_promotion_artifact(
        artifact, confirmed_digest=confirmed_digest, confirmed=True,
    )
    if not applied.get("ok"):
        restored = command(["git", "switch", base])
        removed = command(["git", "branch", "-d", branch]) if restored.returncode == 0 else None
        current_after = command(["git", "symbolic-ref", "--quiet", "--short", "HEAD"])
        result["errors"] = applied.get("errors", [_issue("promotion_apply_failed", "reviewed write-set could not be applied")])
        result["apply"] = applied
        result["cleanup"] = {
            "base_switch_ok": restored.returncode == 0,
            "branch_delete_ok": removed is not None and removed.returncode == 0,
            "current_branch": current_after.stdout.strip() if current_after.returncode == 0 else None,
        }
        if not result["cleanup"]["base_switch_ok"] or not result["cleanup"]["branch_delete_ok"]:
            result["recovery"] = "inspect the dedicated branch and promotion recovery journal before retrying"
        return result

    added = command(["git", "add", "--", *staged_paths])
    if added.returncode != 0:
        result["errors"] = [_issue("publication_stage_failed", "could not stage the reviewed write-set")]
        result["recovery"] = "review local changes on the dedicated branch; no commit or push occurred"
        return result
    diff = command(["git", "diff", "--cached", "--quiet"])
    if diff.returncode == 0:
        result["errors"] = [_issue("publication_empty", "reviewed promotion produced no staged change")]
        return result
    if diff.returncode != 1:
        result["errors"] = [_issue("publication_diff_failed", "could not inspect the staged promotion")]
        return result
    cached = command(["git", "diff", "--cached", "--name-only", "-z"])
    cached_paths = sorted(path for path in cached.stdout.split("\0") if path)
    if cached.returncode != 0 or cached_paths != staged_paths:
        result["errors"] = [_issue("publication_index_mismatch", "Git index contains paths outside the reviewed write-set")]
        result["recovery"] = "review staged changes on the dedicated branch; no commit or push occurred"
        return result
    expected_blobs: dict[str, str] = {}
    content_by_path = {
        path.resolve().relative_to(git_root).as_posix(): content
        for path, content in write_set.items()
    }
    for path in staged_paths:
        expected = command(
            ["git", "hash-object", "--stdin"],
            input_text=content_by_path[path].decode("utf-8"),
        )
        staged_blob = command(["git", "rev-parse", f":{path}"])
        expected_oid = expected.stdout.strip().lower()
        if expected.returncode != 0 or staged_blob.returncode != 0 \
                or staged_blob.stdout.strip().lower() != expected_oid:
            result["errors"] = [_issue("publication_content_mismatch", "staged bytes differ from the reviewed write-set")]
            result["recovery"] = "review staged changes on the dedicated branch; no commit or push occurred"
            return result
        expected_blobs[path] = expected_oid
    message = f"docs(knowledge): promote {plan['target']}"
    commit_command = ["git", "commit", "-m", message] if run_git_hooks else [
        "git", "-c", "core.hooksPath=/dev/null", "commit", "-m", message,
    ]
    committed = command(commit_command)
    if committed.returncode != 0:
        result["errors"] = [_issue("publication_commit_failed", "could not commit the reviewed promotion")]
        result["recovery"] = "review staged changes on the dedicated branch; no push occurred"
        return result
    committed_paths_result = command([
        "git", "diff-tree", "--no-commit-id", "--name-only", "-r", "-z", "HEAD",
    ])
    committed_paths = sorted(path for path in committed_paths_result.stdout.split("\0") if path)
    if committed_paths_result.returncode != 0 or committed_paths != staged_paths:
        result["errors"] = [_issue("publication_commit_mismatch", "local commit contains paths outside the reviewed write-set")]
        result["recovery"] = "the local commit remains on the dedicated branch and was not pushed"
        return result
    for path, expected_oid in expected_blobs.items():
        committed_blob = command(["git", "rev-parse", f"HEAD:{path}"])
        if committed_blob.returncode != 0 or committed_blob.stdout.strip().lower() != expected_oid:
            result["errors"] = [_issue("publication_commit_content_mismatch", "committed bytes differ from the reviewed write-set")]
            result["recovery"] = "the local commit remains on the dedicated branch and was not pushed"
            return result
    head_check = command(["git", "rev-parse", "HEAD"])
    parent_check = command(["git", "rev-parse", "HEAD^"])
    head_oid = head_check.stdout.strip().lower()
    if head_check.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40,64}", head_oid) \
            or parent_check.returncode != 0 \
            or parent_check.stdout.strip().lower() != publication.get("base_oid"):
        result["errors"] = [_issue("publication_commit_base_mismatch", "local publication commit is not based on the reviewed commit")]
        result["recovery"] = "the local commit remains on the dedicated branch and was not pushed"
        return result
    base_check = command(["git", "rev-parse", base])
    remote_check = command(["git", "remote", "get-url", "origin"])
    normalized_remote = _normalize_github_remote(remote_check.stdout) if remote_check.returncode == 0 else None
    if base_check.returncode != 0 or base_check.stdout.strip().lower() != publication.get("base_oid") \
            or normalized_remote is None or normalized_remote.get("repository") != publication.get("repository") \
            or normalized_remote.get("remote_url") != publication.get("remote_url"):
        result["errors"] = [_issue("publication_destination_changed", "base or remote changed after commit; push refused")]
        result["recovery"] = "the local commit remains on the dedicated branch and was not pushed"
        return result
    push_command = [
        "git", "push", publication["remote_url"],
        f"{head_oid}:refs/heads/{branch}",
    ] if run_git_hooks else [
        "git", "-c", "core.hooksPath=/dev/null", "push", "--no-verify",
        publication["remote_url"], f"{head_oid}:refs/heads/{branch}",
    ]
    pushed = command(push_command)
    if pushed.returncode != 0:
        result["errors"] = [_issue("publication_push_failed", "could not push the dedicated promotion branch")]
        result["recovery"] = "the local commit remains on the dedicated branch"
        return result
    evidence_lines = "\n".join(
        f"- `{item['path']}` ({item['sha256']})" for item in plan.get("evidence", [])
    )
    body = (
        "Canonical knowledge promotion from a digest-bound reviewed plan.\n\n"
        f"Plan digest: `{confirmed_digest}`\n\nEvidence:\n{evidence_lines or '- none'}\n\n"
        "This command opened a draft review request. It did not merge the change.\n"
    )
    opened = command([
        "gh", "pr", "create", "--draft", "--base", base, "--head", branch,
        "--repo", publication["repository"], "--title", message, "--body-file", "-",
    ], input_text=body)
    pr_url = opened.stdout.strip()
    if opened.returncode != 0 or not pr_url:
        result["errors"] = [_issue("publication_pr_failed", "branch was pushed but the draft pull request could not be opened")]
        result["recovery"] = "open a draft pull request from the pushed dedicated branch"
        return result
    result.update({
        "ok": True, "errors": [], "changed": applied.get("changed", []),
        "staged": staged_paths, "pr_url": pr_url,
    })
    return result


def _render_human(report: dict[str, Any]) -> str:
    operation = report.get("operation", "knowledge")
    if operation == "lint":
        heading = f"knowledge lint: {'PASS' if report.get('ok') else 'FAIL'}"
        lines = [heading]
        for item in report.get("blocking", []):
            lines.append(f"  ERROR {item['code']}: {item.get('path', '')} {item['message']}".rstrip())
        for item in report.get("advisory", []):
            lines.append(f"  WARN {item['code']}: {item.get('path', '')} {item['message']}".rstrip())
    else:
        label = {
            "init": "knowledge init", "promote-plan": "promotion plan",
            "promote-apply": "promotion apply", "promote-publish": "promotion publish",
        }.get(operation, operation)
        lines = [f"{label}: {'PASS' if report.get('ok') else 'FAIL'}"]
        if report.get("target"):
            lines.append(f"  target: {report['target']}")
        if report.get("promotion_mode"):
            lines.append(f"  mode: {report['promotion_mode']}")
        for key in ("created", "updated", "collisions", "drifted", "changed"):
            if report.get(key):
                lines.append(f"  {key}: {', '.join(report[key])}")
    for item in report.get("errors", []):
        lines.append(f"  ERROR {item['code']}: {item['message']}")
    return "\n".join(lines) + "\n"


def _add_plan_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--start", default=".")
    parser.add_argument("--source", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--evidence", action="append", default=[])
    parser.add_argument("--classification", choices=["personal", "operational", "canonical"])
    parser.add_argument("--mode", choices=["pull-request", "direct-commit"])
    parser.add_argument("--plan-out", required=True)
    parser.add_argument("--json", action="store_true")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="workspace_knowledge.py")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init")
    init.add_argument("--start", default=".")
    init.add_argument("--with-tickets", action="store_true")
    init.add_argument("--json", action="store_true")
    lint = commands.add_parser("lint")
    lint.add_argument("--start", default=".")
    lint.add_argument("--stale-days", type=int, default=180)
    lint.add_argument("--json", action="store_true")
    promote = commands.add_parser("promote")
    promote_commands = promote.add_subparsers(dest="promote_command", required=True)
    plan = promote_commands.add_parser("plan")
    _add_plan_args(plan)
    apply = promote_commands.add_parser("apply")
    apply.add_argument("--plan", required=True)
    apply.add_argument("--digest", required=True)
    apply.add_argument("--confirm", action="store_true")
    apply.add_argument("--json", action="store_true")
    publish = promote_commands.add_parser("publish")
    publish.add_argument("--plan", required=True)
    publish.add_argument("--digest", required=True)
    publish.add_argument("--confirm", action="store_true")
    publish.add_argument(
        "--run-git-hooks", action="store_true",
        help="explicitly allow repository commit/push hooks during publication",
    )
    publish.add_argument("--json", action="store_true")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = _parser().parse_args((argv or sys.argv)[1:])
    if args.command == "init":
        report = initialize_layout(args.start, include_tickets=args.with_tickets)
    elif args.command == "lint":
        if args.stale_days < 1:
            report = {"operation": "lint", "ok": False, "errors": [_issue("stale_days_invalid", "--stale-days must be positive")], "blocking": [], "advisory": []}
        else:
            report = lint_knowledge(args.start, stale_days=args.stale_days)
    else:
        if args.promote_command == "plan":
            plan = plan_promotion(
                start=args.start, source=args.source, target=args.target,
                evidence=args.evidence, classification=args.classification,
                requested_mode=args.mode,
            )
            report = write_promotion_plan(plan, args.plan_out) if plan.get("ok") else plan
        elif args.promote_command == "apply":
            report = apply_promotion_artifact(
                args.plan, confirmed_digest=args.digest, confirmed=args.confirm,
            )
        else:
            report = publish_promotion_artifact(
                args.plan, confirmed_digest=args.digest, confirmed=args.confirm,
                run_git_hooks=args.run_git_hooks,
            )
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False))
    else:
        sys.stdout.write(_render_human(report))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
