#!/usr/bin/env python3
"""push_retry.py — durable push queue for session-save commits.

Some project repos have no git remote by design, so a bare ``git push`` fails
with exit 128. Rather than fail silently, each save records its HEAD commit to a
backoff retry queue; if a remote is ever configured the queued commits drain on
the next save. This turns an invisible no-op into a visible, recoverable state.

Commands:
  ensure   Attempt push when a destination exists, else enqueue HEAD with
           exponential backoff. Succeeds unless the queue file can't be written.
  drain    Push queued commits when a destination exists; clear on success.
  status   Print the queue (count, oldest entry, reasons).
  clear    Empty the queue (manual cleanup; reports how many were dropped).

The queue holds AT MOST ONE entry: a successful ``git push`` sends every local
commit at once, so only the latest HEAD ever needs to be remembered. When HEAD
moves, the entry is replaced in place — ``first_seen`` and ``attempts`` carry
over so backoff pacing and age reporting stay honest (issue #5: one entry per
save accumulated forever on remoteless repos).

Deliberately local repos opt out of queueing entirely:
  git config asha.localOnly true
makes ``ensure`` return ``skipped_local_only`` instead of enqueueing.

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


def _local_only(repo: Path) -> bool:
    """True when the repo is marked deliberately remoteless
    (``git config asha.localOnly true``).

    Fails OPEN (queueing resumes) but never silently: a key that exists with
    an invalid boolean value (``ture``, ``enabled``) makes git exit 128 with
    the stderr discarded — warn so the typo is discoverable (review finding).
    """
    rc, val, _ = _git(repo, "config", "--bool", "--get", "asha.localOnly")
    if rc == 0:
        return val == "true"
    raw_rc, raw_val, _ = _git(repo, "config", "--get", "asha.localOnly")
    if raw_rc == 0:
        print(f"warn: asha.localOnly has invalid boolean value '{raw_val}' — "
              f"treating as false (fix with: git config asha.localOnly true)", file=sys.stderr)
    return False


def _enqueue(qfile: Path, head: str, branch: Optional[str], reason: str, error: str = "") -> dict:
    """Record the latest HEAD as the queue's SINGLE entry.

    A push drains all local commits at once, so multiple entries are always
    redundant. If the entry's head is unchanged this is a retry (attempts
    increments); if HEAD moved, the entry is replaced but ``first_seen`` and
    ``attempts`` carry over — the underlying push problem is the same one, so
    backoff must not reset. Legacy multi-entry queues collapse on first write.
    """
    entries = _read_queue(qfile)
    now = _now()
    # attempts continue from the most recent entry; first_seen comes from the
    # OLDEST (legacy queues appended oldest-first) — 'oldest' in status() must
    # report the true backlog age, not the newest entry's (review finding).
    prior = entries[-1] if entries else None
    oldest = entries[0] if entries else None
    attempts = int(prior.get("attempts", 0)) + 1 if prior else 1
    record = {
        "head": head,
        "branch": branch,
        "reason": reason,
        "attempts": attempts,
        "first_seen": oldest["first_seen"] if oldest and oldest.get("first_seen") else _iso(now),
        "last_attempt": _iso(now),
        "next_retry_after": _iso(now + timedelta(seconds=_backoff_seconds(attempts))),
        "error": error,
    }
    _write_queue(qfile, [record])
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
    if _local_only(repo):
        return {"status": "skipped_local_only", "head": head,
                "queued_total": len(_read_queue(qfile))}
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
        "local_only": _local_only(project_dir),
        "entries": pending,
    }


def clear(project_dir: Path) -> dict:
    """Manual cleanup: empty the queue, report what was dropped."""
    qfile = _queue_path(project_dir)
    pending = _read_queue(qfile)
    if pending:
        _write_queue(qfile, [])
    return {"status": "cleared", "dropped": len(pending)}


def _project_dir(arg: Optional[str]) -> Path:
    if arg:
        return Path(arg).resolve()
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    return Path(env).resolve() if env else Path.cwd()


def main() -> int:
    ap = argparse.ArgumentParser(description="Durable push queue for session-save")
    ap.add_argument("command", choices=["ensure", "drain", "status", "clear"])
    ap.add_argument("--project-dir", "-p", help="Repo whose pushes are managed (default $CLAUDE_PROJECT_DIR/cwd)")
    args = ap.parse_args()
    project_dir = _project_dir(args.project_dir)
    repo = project_dir  # the save target repo is the project root
    if args.command == "ensure":
        result = ensure(repo, project_dir)
    elif args.command == "drain":
        result = drain(repo, project_dir)
    elif args.command == "clear":
        result = clear(project_dir)
    else:
        result = status(project_dir)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
