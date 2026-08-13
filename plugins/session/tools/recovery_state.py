#!/usr/bin/env python3
"""Bounded project-local recovery snapshots for Memory System v2.

Snapshots contain mechanical hints only.  They never write published Memory,
read a host transcript, invoke Git, or perform semantic synthesis.  Every CLI
operation fails open because recovery must not block the host harness.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from path_safety import secure_path, secure_project_root


MAX_BYTES = 2048
MAX_PATHS = 10
TTL_DAYS = 7
_SECRET_PATTERNS = (
    (re.compile(r"(?i)\b(?:Bearer|Basic)\s+[A-Za-z0-9_./+=-]{8,}"), "[REDACTED_AUTH]"),
    (re.compile(r"\b(?:gh[pousr]_|github_pat_|glpat-|npm_|sk-|xox[baprs]-|AIza)[A-Za-z0-9_-]{12,}"), "[REDACTED]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"), "[REDACTED_JWT]"),
    (re.compile(r"\bAKIA[A-Z0-9]{16}\b"), "[REDACTED_AWS_KEY]"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL), "[REDACTED]"),
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _safe(value: Any, fallback: str) -> str:
    text = str(value or fallback)
    text = re.sub(r"[^A-Za-z0-9_.-]", "_", text)[:120]
    return text or fallback


def _filename_component(value: Any, fallback: str) -> str:
    raw = str(value or fallback)
    if _redact(raw) != raw:
        digest = hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:12]
        return f"redacted-{digest}"
    clean = re.sub(r"[^A-Za-z0-9_.-]", "_", raw) or fallback
    if len(clean) <= 70 and clean == raw:
        return clean
    digest = hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:12]
    return f"{clean[:57]}-{digest}"


def _redact(value: Any) -> str:
    text = str(value or "").replace("\x00", "")
    # URL/DSN userinfo may contain credentials without an assignment label.
    text = re.sub(
        r"(?i)\b([a-z][a-z0-9+.-]*://)[^/@\s]+@",
        lambda m: f"{m.group(1)}[REDACTED]@",
        text,
    )
    # Assignment secrets retain the key but never the value.
    text = re.sub(
        r"(?i)\b((?=[a-z0-9_-]*(?:api[_-]?key|token|secret|password|passwd|"
        r"authorization|credentials?|auth|bearer))[a-z_][a-z0-9_-]*)"
        r"\s*[:=]\s*(?:(?:Bearer|Basic)\s+)?(?:\"[^\"\n]*\"|'[^'\n]*'|[^\s,;|&]+)",
        lambda m: f"{m.group(1)}=[REDACTED]",
        text,
    )
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _root(project_dir: Path) -> Path:
    root = secure_project_root(project_dir, reject_home=True)
    if secure_path(root, "Work/markers/silence").exists():
        raise ValueError("Memory persistence is disabled by Work/markers/silence")
    cfg = secure_path(root, ".asha/config.json")
    data = json.loads(cfg.read_text(encoding="utf-8"))
    if (not isinstance(data, dict) or data.get("memory_version") != 2 or
            not data.get("project_id")):
        raise ValueError("initialized Memory v2 project with project_id required")
    return root


def _path(root: Path, harness: Any, session_id: Any) -> Path:
    if not harness or not session_id:
        raise ValueError("recovery harness and session identity are required")
    directory = secure_path(root, "Work/session-state/.keep", create_parents=True).parent
    # Leave headroom for the adjacent .lock and same-directory temporary names
    # beneath common 255-byte NAME_MAX filesystems.
    safe_harness = _filename_component(harness, "unknown")
    safe_session = _filename_component(session_id, "unknown")
    path = directory / f"{safe_harness}-{safe_session}.json"
    if not path.parent.resolve(strict=True).is_relative_to(root):
        raise ValueError("snapshot path escapes project recovery directory")
    secure_path(root, path.relative_to(root))
    return path


def _directory(root: Path) -> Path:
    return secure_path(root, "Work/session-state/.keep", create_parents=True).parent


def _global_lock_target(root: Path) -> Path:
    return _directory(root) / ".recovery-global"


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_suffix(path.suffix + ".lock")
    if lock.is_symlink():
        raise ValueError(f"symlinked recovery lock rejected: {lock}")
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _atomic_json(path: Path, data: dict[str, Any]) -> None:
    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n"
    if len(raw.encode("utf-8")) > MAX_BYTES:
        raise ValueError("recovery snapshot exceeds 2048 bytes")
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.tmp.", dir=path.parent)
    tmp = Path(name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o600)
        try:
            dir_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def _read(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _validated_snapshot_paths(root: Path) -> list[Path]:
    """Return only files whose name and embedded identity form a v2 snapshot."""
    directory = _directory(root)
    result: list[Path] = []
    if not directory.is_dir():
        return result
    config = json.loads((root / ".asha/config.json").read_text(encoding="utf-8"))
    for path in directory.glob("*.json"):
        if path.name.startswith(".") or not path.is_file() or path.is_symlink():
            continue
        if not re.fullmatch(r"[A-Za-z0-9_.-]+-[A-Za-z0-9_.-]+\.json", path.name):
            continue
        data = _read(path)
        harness = data.get("harness")
        session_id = data.get("session_id")
        harness_digest = str(data.get("harness_sha256", ""))
        session_digest = str(data.get("session_sha256", ""))
        harness_component = str(data.get("harness_component", ""))
        session_component = str(data.get("session_component", ""))
        if (data.get("version") != 2 or data.get("project_id") != config.get("project_id") or
                data.get("snapshot_name") != path.name or
                not re.fullmatch(r"[0-9a-f]{64}", str(data.get("harness_sha256", ""))) or
                not re.fullmatch(r"[0-9a-f]{64}", str(data.get("session_sha256", ""))) or
                not harness or not session_id or
                path.name != f"{harness_component}-{session_component}.json"):
            continue
        for stored, digest, component, redacted, truncated, fallback in (
            (harness, harness_digest, harness_component, data.get("harness_redacted"),
             data.get("harness_truncated"), "unknown"),
            (session_id, session_digest, session_component, data.get("session_redacted"),
             data.get("session_truncated"), "unknown"),
        ):
            if not isinstance(truncated, bool):
                break
            if redacted is True:
                if component != f"redacted-{digest[:12]}":
                    break
            elif truncated:
                if not component.endswith(f"-{digest[:12]}"):
                    break
            elif redacted is False:
                if (hashlib.sha256(str(stored).encode("utf-8")).hexdigest() != digest or
                        _filename_component(stored, fallback) != component):
                    break
            else:
                break
        else:
            result.append(path)
    return result


def _paths(payload: dict[str, Any]) -> list[str]:
    raw = payload.get("paths") or []
    if isinstance(raw, str):
        raw = [raw]
    elif not isinstance(raw, (list, tuple)):
        raw = []
    tool_input = payload.get("tool_input")
    if tool_input is None:
        tool_input = payload.get("toolArgs")
        if isinstance(tool_input, str):
            try:
                tool_input = json.loads(tool_input)
            except json.JSONDecodeError:
                tool_input = {}
    if isinstance(tool_input, dict):
        pending: list[Any] = [tool_input]
        while pending:
            value = pending.pop()
            if isinstance(value, dict):
                for key, item in value.items():
                    if key in ("file_path", "filePath", "path", "paths"):
                        if isinstance(item, (list, tuple)):
                            raw = [*raw, *item]
                        elif item:
                            raw = [*raw, item]
                    elif isinstance(item, (dict, list, tuple)):
                        pending.append(item)
            elif isinstance(value, (list, tuple)):
                pending.extend(value)
    seen: set[str] = set()
    result: list[str] = []
    for item in raw:
        item = _redact(item).strip()[:240]
        if item and item not in seen:
            seen.add(item)
            result.append(item)
        if len(result) == MAX_PATHS:
            break
    return result


def update(project_dir: Path, payload: Any) -> Path | None:
    """Merge one normalized prompt/tool/start event.  Any failure is a no-op."""
    if not isinstance(payload, dict):
        return None
    try:
        root = _root(project_dir)
        harness = payload.get("harness")
        session_id = payload.get("session_id") or payload.get("sessionId")
        path = _path(root, harness, session_id)
        with _locked(_global_lock_target(root)), _locked(path):
            previous = _read(path)
            stored_harness = _redact(harness)[:120]
            stored_session = _redact(session_id)[:160]
            harness_redacted = _redact(harness) != str(harness)
            session_redacted = _redact(session_id) != str(session_id)
            if previous and (previous.get("harness") != stored_harness or
                             previous.get("session_id") != stored_session):
                raise ValueError("recovery identity collision")
            now = _now()
            cfg = json.loads((root / ".asha/config.json").read_text())
            incoming_paths = _paths(payload)
            all_paths = []
            for item in [*(previous.get("paths") or []), *incoming_paths]:
                if item not in all_paths:
                    all_paths.append(item)
            all_paths = all_paths[-MAX_PATHS:]
            event = str(payload.get("event") or "update")
            prompt = payload.get("prompt") or payload.get("initialPrompt")
            action = payload.get("tool_name") or payload.get("toolName") or event
            blocker = (payload.get("blocker") or payload.get("error") or
                       payload.get("errorMessage") or payload.get("errors"))
            tool_result = payload.get("toolResult")
            if not blocker and isinstance(tool_result, dict):
                blocker = (tool_result.get("error") or tool_result.get("errorMessage") or
                           tool_result.get("stderr") or tool_result.get("errors"))
            elif not blocker and isinstance(tool_result, str) and payload.get("isError"):
                blocker = tool_result
            data = {
                "version": 2,
                "snapshot_name": path.name,
                "harness_sha256": hashlib.sha256(str(harness).encode("utf-8", errors="replace")).hexdigest(),
                "session_sha256": hashlib.sha256(str(session_id).encode("utf-8", errors="replace")).hexdigest(),
                "harness_component": _filename_component(harness, "unknown"),
                "session_component": _filename_component(session_id, "unknown"),
                "harness_redacted": harness_redacted,
                "session_redacted": session_redacted,
                "harness_truncated": not harness_redacted and len(_redact(harness)) > 120,
                "session_truncated": not session_redacted and len(_redact(session_id)) > 160,
                "session_id": stored_session,
                "project_id": _redact(cfg["project_id"])[:160],
                "harness": stored_harness,
                "created_at": previous.get("created_at") or now,
                "updated_at": now,
                "prompt": _redact(prompt if prompt is not None else previous.get("prompt", ""))[:900],
                "last_action": _redact(action)[:240],
                "paths": all_paths,
                "blocker": _redact(blocker if blocker is not None else previous.get("blocker", ""))[:320],
            }
            if event == "end":
                data["sealed_at"] = now
            elif previous.get("sealed_at"):
                data["sealed_at"] = previous["sealed_at"]
            # Deterministically shrink optional hints until the hard cap holds.
            for key in ("prompt", "blocker", "last_action"):
                while len((json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n").encode()) > MAX_BYTES and data[key]:
                    data[key] = data[key][:-64]
            while len((json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n").encode()) > MAX_BYTES and data["paths"]:
                data["paths"].pop(0)
            _atomic_json(path, data)
            return path
    except Exception:
        return None


def sweep(project_dir: Path, days: int = TTL_DAYS) -> list[Path]:
    removed: list[Path] = []
    try:
        root = _root(project_dir)
        directory = _directory(root)
        cutoff = datetime.now(timezone.utc).timestamp() - timedelta(days=days).total_seconds()
        with _locked(_global_lock_target(root)):
            for path in _validated_snapshot_paths(root):
                lock = path.with_suffix(path.suffix + ".lock")
                with _locked(path):
                    if path.exists() and path.stat().st_mtime < cutoff:
                        path.unlink(missing_ok=True)
                        removed.append(path)
                if not path.exists():
                    lock.unlink(missing_ok=True)
            valid_lock_names = {path.name + ".lock" for path in _validated_snapshot_paths(root)}
            for lock in directory.glob("*.json.lock"):
                if lock.name not in valid_lock_names:
                    continue
                snapshot = Path(str(lock)[:-5])
                if not snapshot.exists() and lock.stat().st_mtime < cutoff:
                    lock.unlink(missing_ok=True)
    except Exception:
        pass
    return removed


def seal(project_dir: Path, harness: str, session_id: str) -> Path | None:
    try:
        root = _root(project_dir)
        path = _path(root, harness, session_id)
        with _locked(_global_lock_target(root)), _locked(path):
            prior = _read(path)
            now = _now()
            cfg = json.loads((root / ".asha/config.json").read_text(encoding="utf-8"))
            stored_harness = _redact(harness)[:120]
            stored_session = _redact(session_id)[:160]
            harness_redacted = _redact(harness) != str(harness)
            session_redacted = _redact(session_id) != str(session_id)
            result_data = prior or {
                "version": 2, "snapshot_name": path.name,
                "harness_sha256": hashlib.sha256(str(harness).encode("utf-8", errors="replace")).hexdigest(),
                "session_sha256": hashlib.sha256(str(session_id).encode("utf-8", errors="replace")).hexdigest(),
                "harness_component": _filename_component(harness, "unknown"),
                "session_component": _filename_component(session_id, "unknown"),
                "harness_redacted": harness_redacted,
                "session_redacted": session_redacted,
                "harness_truncated": not harness_redacted and len(_redact(harness)) > 120,
                "session_truncated": not session_redacted and len(_redact(session_id)) > 160,
                "session_id": stored_session,
                "project_id": _redact(cfg["project_id"])[:160], "harness": stored_harness,
                "created_at": now, "prompt": "", "last_action": "",
                "paths": [], "blocker": "",
            }
            if (result_data.get("harness") != stored_harness or
                    result_data.get("session_id") != stored_session):
                raise ValueError("recovery identity collision")
            result_data["updated_at"] = now
            result_data["sealed_at"] = now
            _atomic_json(path, result_data)
            result = path
        sweep(root)
        return result
    except Exception:
        return None


def latest(project_dir: Path) -> dict[str, Any] | None:
    try:
        root = _root(project_dir)
        directory = _directory(root)
        with _locked(_global_lock_target(root)):
            files = _validated_snapshot_paths(root)
            return _read(max(files, key=lambda item: item.stat().st_mtime)) if files else None
    except Exception:
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fail-open Memory v2 recovery state")
    parser.add_argument("operation", choices=("update", "start", "seal", "sweep", "latest"))
    parser.add_argument("--project-dir", required=True, type=Path)
    parser.add_argument("--harness", default=os.environ.get("ASHA_HARNESS", ""))
    parser.add_argument("--session-id", default=os.environ.get("ASHA_SESSION_ID", ""))
    args = parser.parse_args(argv)
    try:
        if args.operation in ("update", "start"):
            try:
                payload = json.load(os.sys.stdin)
            except Exception:
                payload = {}
            if not isinstance(payload, dict):
                return 0
            if args.harness and args.harness != "unknown":
                payload.setdefault("harness", args.harness)
            if args.session_id and args.session_id != "unknown":
                payload.setdefault("session_id", args.session_id)
            payload.setdefault("event", args.operation)
            update(args.project_dir, payload)
        elif args.operation == "seal":
            seal(args.project_dir, args.harness, args.session_id)
        elif args.operation == "sweep":
            sweep(args.project_dir)
        else:
            value = latest(args.project_dir)
            if value:
                print(json.dumps(value, ensure_ascii=False))
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
