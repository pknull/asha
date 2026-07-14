#!/usr/bin/env python3
"""Build and query the compact memory-nudge index. Always fails open at CLI."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import signal
import stat
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from memory_retrieval import (
    discover_memory_dirs, dump_index, load_index, rank,
)


def _index_path(project: Path | None = None) -> Path:
    project = (project or Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())).resolve()
    key = hashlib.sha256(str(project).encode("utf-8")).hexdigest()[:16]
    return Path.home() / ".cache" / "asha" / f"nudge-index-{key}.json"


def build(args) -> None:
    project = Path(args.project_dir).resolve()
    # Runtime context injection never crosses project trust boundaries. Global
    # learnings remain available, but another project's authored memory does not.
    dirs = discover_memory_dirs(project, all_projects=False)
    dump_index(Path(args.index) if args.index else _index_path(project), dirs,
               Path(args.learnings_dir).expanduser())


def _match_text(payload: dict) -> tuple[str, str]:
    tool = str(payload.get("tool_name") or "")
    tool_input = payload.get("tool_input") or {}
    if tool == "Grep":
        return tool, str(tool_input.get("pattern") or "")
    if tool == "Bash":
        return tool, str(tool_input.get("command") or "")[:200]
    if tool == "WebSearch":
        return tool, str(tool_input.get("query") or "")
    return tool, ""


def _safe_session(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", value)[:120] or "unknown"


def _log(path: Path, event: dict) -> None:
    try:
        path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        if path.is_symlink():
            return
        event = {"timestamp": datetime.now(timezone.utc).isoformat(), **event}
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(path, flags, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            handle.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
            handle.flush()
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass  # Metrics are subordinate to the fail-open nudge path.


def _secure_state_dir(path: Path) -> None:
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise PermissionError(f"unsafe nudge state directory: {path}")
    os.chmod(path, 0o700)


def _default_state_dir() -> str:
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    base = Path(runtime) if runtime else Path("/tmp")
    return str(base / f"asha-memory-nudge-{os.getuid()}")


def _mutate_state(state_file: Path, callback):
    _secure_state_dir(state_file.parent)
    lock = state_file.with_suffix(".lock")
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    lock_fd = os.open(lock, flags, 0o600)
    os.fchmod(lock_fd, 0o600)
    with os.fdopen(lock_fd, "a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            if state_file.is_symlink():
                raise PermissionError(f"unsafe nudge state file: {state_file}")
            state = json.loads(state_file.read_text(encoding="utf-8")) if state_file.is_file() else {}
        except (OSError, ValueError, TypeError):
            state = {}
        result = callback(state)
        tmp_fd, tmp_name = tempfile.mkstemp(prefix=f".{state_file.name}.", dir=state_file.parent)
        tmp = Path(tmp_name)
        try:
            os.fchmod(tmp_fd, 0o600)
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as tmp_handle:
                json.dump(state, tmp_handle, separators=(",", ":"))
                tmp_handle.flush()
            os.replace(tmp, state_file)
        finally:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return result


def match(args) -> None:
    payload = json.load(sys.stdin)
    tool, query = _match_text(payload)
    if not query:
        return
    entries = load_index(Path(args.index) if args.index else _index_path())
    ranked = rank(query, entries, limit=2)
    if not ranked:
        return
    best = ranked[0]
    overlaps = best["overlap"]
    # High precision: two catalogue-token matches, or one distinctive rare token.
    rare_limit = max(2, int(best["corpus_size"] * 0.02))
    distinctive = (len(overlaps) == 1 and len(overlaps[0]) >= 7
                   and best["min_overlap_df"] <= rare_limit)
    if best["score"] < args.threshold or (len(overlaps) < 2 and not distinctive):
        return
    if len(ranked) > 1 and best["score"] < ranked[1]["score"] * 1.12 and not distinctive:
        return

    session = _safe_session(str(payload.get("session_id") or os.environ.get("CLAUDE_CODE_SESSION_ID") or "unknown"))
    state_file = Path(args.state_dir) / f"{session}.json"

    def decide(state: dict):
        fired = state.setdefault("fired", {})
        count = int(state.get("count", 0))
        if best["id"] in fired:
            return "duplicate"
        if count >= args.cap:
            if not state.get("cap_reported"):
                state["cap_reported"] = True
                return "capped"
            return "cap-silent"
        fired[best["id"]] = {"path": best["path"], "acted": False}
        state["count"] = count + 1
        return "fire"

    decision = _mutate_state(state_file, decide)
    if decision == "capped":
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
              "additionalContext": "memory-nudge: session cap reached; further nudges suppressed"}}))
        return
    if decision != "fire":
        return
    _log(Path(args.log), {"event": "memory_nudge", "status": "fired", "session_id": session,
         "tool": tool, "memory_id": best["id"], "path": best["path"], "score": best["score"]})
    description = best["description"]
    if len(description) > 180:
        description = description[:177].rsplit(" ", 1)[0].rstrip() + "..."
    context = f"memory-nudge: possibly answered by {best['id']} — {description} (Read {best['path']})"
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": context}}))


def acted(args) -> None:
    payload = json.load(sys.stdin)
    if str(payload.get("tool_name") or "") != "Read":
        return
    read_path = str((payload.get("tool_input") or {}).get("file_path") or "")
    if not read_path:
        return
    session = _safe_session(str(payload.get("session_id") or os.environ.get("CLAUDE_CODE_SESSION_ID") or "unknown"))
    state_file = Path(args.state_dir) / f"{session}.json"
    acted_ids: list[str] = []

    def mark(state: dict):
        for memory_id, item in state.get("fired", {}).items():
            if not item.get("acted") and os.path.realpath(str(item.get("path", ""))) == os.path.realpath(read_path):
                item["acted"] = True
                acted_ids.append(memory_id)

    _mutate_state(state_file, mark)
    for memory_id in acted_ids:
        _log(Path(args.log), {"event": "memory_nudge", "status": "acted", "session_id": session,
             "memory_id": memory_id, "path": read_path})


def stats(args) -> None:
    path = Path(args.log)
    fired: set[tuple[str, str]] = set()
    acted_on: set[tuple[str, str]] = set()
    cutoff = datetime.now(timezone.utc).timestamp() - args.days * 86400
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for line in lines:
        try:
            event = json.loads(line)
            timestamp = datetime.fromisoformat(str(event["timestamp"]).replace("Z", "+00:00")).timestamp()
            if timestamp < cutoff:
                continue
            key = (str(event.get("session_id") or ""), str(event.get("memory_id") or ""))
            if event.get("status") == "fired":
                fired.add(key)
            elif event.get("status") == "acted":
                acted_on.add(key)
        except (ValueError, TypeError, KeyError, json.JSONDecodeError):
            continue
    acted_count = len(fired & acted_on)
    result = {"days": args.days, "fired": len(fired), "acted": acted_count,
              "acted_rate": round(acted_count / len(fired), 4) if fired else 0.0}
    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        print(f"memory nudges ({args.days}d): {len(fired)} fired, {acted_count} acted-on "
              f"({result['acted_rate'] * 100:.1f}%)")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser()
    root.add_argument("--index")
    root.add_argument("--learnings-dir", default=str(Path.home() / ".asha" / "learnings"))
    root.add_argument("--state-dir", default=_default_state_dir())
    root.add_argument("--log", default=str(Path.home() / ".cache" / "asha" / "nudge-events.jsonl"))
    sub = root.add_subparsers(dest="command", required=True)
    build_parser = sub.add_parser("build")
    build_parser.add_argument("--project-dir", default=os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
    match_parser = sub.add_parser("match")
    match_parser.add_argument("--threshold", type=float, default=0.28)
    match_parser.add_argument("--cap", type=int, default=5)
    sub.add_parser("acted")
    stats_parser = sub.add_parser("stats")
    stats_parser.add_argument("--days", type=int, default=7)
    stats_parser.add_argument("--json", action="store_true")
    return root


def _timeout(_signum, _frame) -> None:
    raise TimeoutError


def main() -> int:
    try:
        args = parser().parse_args()
        if args.command == "match" and hasattr(signal, "setitimer"):
            signal.signal(signal.SIGALRM, _timeout)
            signal.setitimer(signal.ITIMER_REAL, 0.095)
        try:
            {"build": build, "match": match, "acted": acted, "stats": stats}[args.command](args)
        finally:
            if args.command == "match" and hasattr(signal, "setitimer"):
                signal.setitimer(signal.ITIMER_REAL, 0)
    except Exception:
        pass  # The hook contract is fail-open and silent on every internal error.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
