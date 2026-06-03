#!/usr/bin/env python3
"""push_retry.py — durable push queue for session-save commits.

The ~/life repo has no git remote by design, so a bare ``git push`` fails with
exit 128. Rather than fail silently, each save records its HEAD commit to a
backoff retry queue; if a remote is ever configured the queued commits drain on
the next save. This turns an invisible no-op into a visible, recoverable state.

Commands:
  ensure   Attempt push when a destination exists, else enqueue HEAD with
           exponential backoff. Succeeds unless the queue file can't be written.
  drain    Push queued commits when a destination exists; clear on success.
  status   Print the queue (count, oldest entry, reasons).

The queue lives at ``<project>/Memory/events/push-queue.jsonl`` (gitignored, and
never staged — /save only ``git add Memory/`` of tracked files). Each line:
  {"head", "branch", "reason", "attempts", "first_seen", "last_attempt",
   "next_retry_after", "error"}
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

BACKOFF_BASE_SECONDS = 60
BACKOFF_CAP_SECONDS = 6 * 60 * 60  # 6h


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _git(repo: Path, *args: str, check: bool = False) -> tuple[int, str, str]:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def _queue_path(project_dir: Path) -> Path:
    return project_dir / "Memory" / "events" / "push-queue.jsonl"


def _read_queue(qfile: Path) -> list[dict]:
    if not qfile.exists():
        return []
    out = []
    for line in qfile.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _write_queue(qfile: Path, entries: list[dict]) -> None:
    qfile.parent.mkdir(parents=True, exist_ok=True)
    tmp = qfile.with_suffix(qfile.suffix + ".tmp")
    tmp.write_text("".join(json.dumps(e) + "\n" for e in entries))
    tmp.replace(qfile)


def _backoff_seconds(attempts: int) -> int:
    return min(BACKOFF_CAP_SECONDS, BACKOFF_BASE_SECONDS * (2 ** max(0, attempts - 1)))


def push_destination(repo: Path) -> tuple[bool, Optional[str], Optional[str], Optional[str]]:
    """(has_destination, remote, branch, upstream)."""
    rc, remotes, _ = _git(repo, "remote")
    remote_list = remotes.split() if remotes else []
    if not remote_list:
        return (False, None, None, None)
    _, branch, _ = _git(repo, "branch", "--show-current")
    rc, up, _ = _git(repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")
    if rc != 0 or not up:
        return (False, remote_list[0], branch, None)
    return (True, remote_list[0], branch, up)


def _try_push(repo: Path) -> tuple[bool, str]:
    rc, _, err = _git(repo, "push")
    return (rc == 0, err)


def _enqueue(qfile: Path, head: str, branch: Optional[str], reason: str, error: str = "") -> dict:
    entries = _read_queue(qfile)
    now = _now()
    existing = next((e for e in entries if e.get("head") == head), None)
    if existing:
        existing["attempts"] = int(existing.get("attempts", 0)) + 1
        existing["last_attempt"] = _iso(now)
        existing["reason"] = reason
        existing["error"] = error
        existing["next_retry_after"] = _iso(now + timedelta(seconds=_backoff_seconds(existing["attempts"])))
        record = existing
    else:
        record = {
            "head": head,
            "branch": branch,
            "reason": reason,
            "attempts": 1,
            "first_seen": _iso(now),
            "last_attempt": _iso(now),
            "next_retry_after": _iso(now + timedelta(seconds=_backoff_seconds(1))),
            "error": error,
        }
        entries.append(record)
    _write_queue(qfile, entries)
    return record


def ensure(repo: Path, project_dir: Path) -> dict:
    """Push if possible, else queue HEAD. Never raises on a missing remote."""
    qfile = _queue_path(project_dir)
    rc, head, _ = _git(repo, "rev-parse", "HEAD")
    if rc != 0:
        return {"status": "error", "reason": "no_head", "head": None}
    has_dest, remote, branch, _up = push_destination(repo)
    if has_dest:
        ok, err = _try_push(repo)
        if ok:
            _write_queue(qfile, [])  # push sends all local commits; queue drains
            return {"status": "pushed", "head": head, "remote": remote}
        rec = _enqueue(qfile, head, branch, "push_failed", err)
        return {"status": "queued", "reason": "push_failed", "head": head,
                "error": err, "next_retry_after": rec["next_retry_after"]}
    reason = "no_remote" if not remote else "no_upstream"
    rec = _enqueue(qfile, head, branch, reason)
    return {"status": "queued", "reason": reason, "head": head,
            "queued_total": len(_read_queue(qfile)),
            "next_retry_after": rec["next_retry_after"]}


def drain(repo: Path, project_dir: Path) -> dict:
    qfile = _queue_path(project_dir)
    pending = _read_queue(qfile)
    if not pending:
        return {"status": "empty", "drained": 0}
    has_dest, remote, _b, _u = push_destination(repo)
    if not has_dest:
        return {"status": "no_destination", "pending": len(pending)}
    ok, err = _try_push(repo)
    if ok:
        _write_queue(qfile, [])
        return {"status": "drained", "drained": len(pending), "remote": remote}
    return {"status": "push_failed", "pending": len(pending), "error": err}


def status(project_dir: Path) -> dict:
    pending = _read_queue(_queue_path(project_dir))
    return {
        "queued_total": len(pending),
        "oldest": pending[0]["first_seen"] if pending else None,
        "reasons": sorted({e.get("reason", "?") for e in pending}),
        "entries": pending,
    }


def _project_dir(arg: Optional[str]) -> Path:
    if arg:
        return Path(arg).resolve()
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    return Path(env).resolve() if env else Path.cwd()


def main() -> int:
    ap = argparse.ArgumentParser(description="Durable push queue for session-save")
    ap.add_argument("command", choices=["ensure", "drain", "status"])
    ap.add_argument("--project-dir", "-p", help="Repo whose pushes are managed (default $CLAUDE_PROJECT_DIR/cwd)")
    args = ap.parse_args()
    project_dir = _project_dir(args.project_dir)
    repo = project_dir  # the save target repo is the project root
    if args.command == "ensure":
        result = ensure(repo, project_dir)
    elif args.command == "drain":
        result = drain(repo, project_dir)
    else:
        result = status(project_dir)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
