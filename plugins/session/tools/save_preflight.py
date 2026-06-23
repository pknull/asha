#!/usr/bin/env python3
"""save_preflight.py — pre-flight verification gate for /session:save.

Runs four gates before a session save is allowed to complete and logs each
result. Three invocation modes:

  --mode enforce   Stop-hook enforcement. Always exits 0; prints a machine JSON
                   result {hard_fail, reason, gates} on STDOUT (consumed by the
                   Stop hook, which turns hard_fail into a Claude block) and a
                   human table on STDERR.
  --mode guard     Inline pre-commit / automatic-save guard. Prints the human
                   table on STDOUT; exits 1 on hard fail so the caller aborts the
                   write/commit, else 0.
  --mode report    Dry run. Prints the human table on STDOUT; exits 0 always.

Gates (each logged to Memory/events/save-preflight.jsonl):
  1 memory_substrate  Memory/ + Memory/events/events.jsonl exist (self-healing).
  2 session_integrity events.jsonl is stamped with THIS session (HARD), and this
    + ac_clobber      save did not write a generic auto-fallback block (HARD,
                      scoped to the save's diff so historical cruft is ignored).
  3 ac_fresh          activeContext.md was updated this save (WARN) and Next Steps
    + ac_handoff      is not the generic stub (WARN).
    + ac_wwa_         the LEAD "What Was Accomplished" section is stamped for THIS
      provenance      session (HARD when the session had real activity but the
                      lead WWA belongs to a foreign/prior session — the bg
                      0-Edit/Write handoff gap; a truly empty session passes).
  4 push_durability   git push has a destination, else HEAD is queued for retry
                      with backoff — never silent (PASS). Delegates to push_retry.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Auto-fallback bullets emitted by pattern_analyzer.synthesize_accomplishments
# when no real signal exists. Their presence in *this save's* diff means a stub
# clobbered curated content.
_AUTO_FALLBACK_RE = re.compile(
    r"Created \d+ file\(s\)|Modified \d+ file\(s\)|No significant changes recorded"
)
_GENERIC_NEXT_STEPS = ("review and plan next session", "continue work in")


@dataclass
class GateResult:
    gate: str
    status: str   # pass | warn | fail
    hard: bool    # a hard fail blocks; a soft fail/warn only logs
    detail: str


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _git(repo: Path, *args: str) -> tuple[int, str, str]:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    return proc.returncode, proc.stdout, proc.stderr


# --------------------------------------------------------------------------- #
# Gate 1 — memory substrate (self-healing)
# --------------------------------------------------------------------------- #
def gate_memory_substrate(project_dir: Path) -> list[GateResult]:
    created = []
    mem = project_dir / "Memory"
    ev_dir = mem / "events"
    ev_file = ev_dir / "events.jsonl"
    for d in (mem, ev_dir):
        if not d.exists():
            d.mkdir(parents=True, exist_ok=True)
            created.append(str(d.relative_to(project_dir)))
    if not ev_file.exists():
        ev_file.touch()
        created.append(str(ev_file.relative_to(project_dir)))
    detail = ("created: " + ", ".join(created)) if created else "Memory/ + events.jsonl present"
    return [GateResult("memory_substrate", "pass", True, detail)]


# --------------------------------------------------------------------------- #
# Gate 2 — session integrity + clobber
# --------------------------------------------------------------------------- #
def gate_session_integrity(project_dir: Path, current_sid: Optional[str]) -> GateResult:
    events_file = Path(os.environ.get("ASHA_EVENTS_FILE") or (project_dir / "Memory" / "events" / "events.jsonl"))
    sids: set[Optional[str]] = set()
    n = 0
    if events_file.exists():
        for line in events_file.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            n += 1
            sids.add(ev.get("session_id"))
    if n == 0:
        return GateResult("session_integrity", "pass", True, "events.jsonl empty; synthesis no-ops (no bleed possible)")
    if not current_sid:
        return GateResult("session_integrity", "warn", False, f"current session id unknown; cannot verify {n} events")
    if current_sid not in sids:
        foreign = sorted(str(s) for s in sids if s)
        return GateResult("session_integrity", "fail", True,
                          f"events.jsonl holds {n} events from {foreign} but NOT current session "
                          f"{current_sid} — synthesized from a foreign/stale transcript")
    foreign = sorted(str(s) for s in sids if s and s != current_sid)
    if foreign:
        return GateResult("session_integrity", "warn", False,
                          f"events.jsonl mixes current session with {foreign} (ok across a multi-save window)")
    return GateResult("session_integrity", "pass", True, f"all {n} events stamped current session {current_sid}")


def _active_context_added_lines(repo: Path) -> list[str]:
    """Lines this save added to activeContext.md (uncommitted vs HEAD; else the
    HEAD commit's diff). Scoping to the diff means the file's historical content
    can never false-trigger the clobber check."""
    rc, out, _ = _git(repo, "diff", "HEAD", "--", "Memory/activeContext.md")
    if not out.strip():
        rc, out, _ = _git(repo, "show", "--format=", "HEAD", "--", "Memory/activeContext.md")
    return [ln[1:] for ln in out.splitlines() if ln.startswith("+") and not ln.startswith("+++")]


def gate_clobber(repo: Path) -> GateResult:
    added = _active_context_added_lines(repo)
    hits = [ln.strip() for ln in added if _AUTO_FALLBACK_RE.search(ln)]
    if hits:
        sample = hits[0][:120]
        return GateResult("ac_clobber", "fail", True,
                          f"this save wrote {len(hits)} generic auto-fallback line(s) into activeContext.md "
                          f"(e.g. \"{sample}\") — curated content was clobbered or synthesis ran on no/foreign events")
    return GateResult("ac_clobber", "pass", True, "no auto-fallback stub added this save")


# --------------------------------------------------------------------------- #
# Gate 3 — activeContext freshness + handoff quality (soft)
# --------------------------------------------------------------------------- #
def _section_body(text: str, header: str) -> Optional[str]:
    m = re.search(rf"^##+\s*{re.escape(header)}.*$", text, re.MULTILINE)
    if not m:
        return None
    start = m.end()
    nxt = re.search(r"^##+\s", text[start:], re.MULTILINE)
    return text[start:start + nxt.start()] if nxt else text[start:]


def gate_active_context(project_dir: Path, save_start: Optional[datetime]) -> list[GateResult]:
    ac = project_dir / "Memory" / "activeContext.md"
    if not ac.exists():
        return [GateResult("ac_fresh", "fail", True, "activeContext.md missing")]
    results: list[GateResult] = []
    mtime = datetime.fromtimestamp(ac.stat().st_mtime, timezone.utc)
    if save_start and mtime < save_start:
        results.append(GateResult("ac_fresh", "warn", False,
                                  f"activeContext.md not rewritten this save (mtime {_iso(mtime)} < save start {_iso(save_start)})"))
    else:
        results.append(GateResult("ac_fresh", "pass", False, "activeContext.md updated this save"))
    ns = _section_body(ac.read_text(), "Next Steps")
    if ns:
        bullets = [b.strip("-*[ ]").strip().lower() for b in ns.splitlines() if b.strip().startswith(("-", "*"))]
        if bullets and all(any(g in b for g in _GENERIC_NEXT_STEPS) for b in bullets):
            results.append(GateResult("ac_handoff", "warn", False,
                                      "Next Steps is the generic stub; replace with actionable cold-start items"))
        else:
            results.append(GateResult("ac_handoff", "pass", False, "Next Steps is actionable"))
    else:
        results.append(GateResult("ac_handoff", "pass", False, "Next Steps section absent (stripped)"))
    return results


# --------------------------------------------------------------------------- #
# Gate 3b — WWA provenance (lead handoff belongs to THIS session)
# --------------------------------------------------------------------------- #
# Matches the stamp pattern_analyzer writes under the lead WWA header. Keep in
# sync with pattern_analyzer.WWA_SESSION_MARKER.
_WWA_HEADER_RE = re.compile(r"^##\s+What Was Accomplished\b.*$", re.MULTILINE)
_WWA_SESSION_RE = re.compile(r"<!--\s*wwa-session:\s*(\S+?)\s*-->")


def _lead_wwa_section(text: str) -> Optional[str]:
    """Body of the FIRST '## What Was Accomplished*' section (the lead handoff a
    cold-start session reads first), or None if there is no such section."""
    m = _WWA_HEADER_RE.search(text)
    if not m:
        return None
    start = m.end()
    nxt = re.search(r"^##\s", text[start:], re.MULTILINE)
    return text[start:start + nxt.start()] if nxt else text[start:]


def _session_event_count(project_dir: Path, current_sid: Optional[str]) -> int:
    """Count events.jsonl entries stamped with current_sid — i.e. whether THIS
    session actually did work (even if all of it went through Bash/RCON and
    produced no Edit/Write events for the synthesizer to narrate)."""
    if not current_sid:
        return 0
    events_file = Path(os.environ.get("ASHA_EVENTS_FILE") or (project_dir / "Memory" / "events" / "events.jsonl"))
    if not events_file.exists():
        return 0
    n = 0
    for line in events_file.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("session_id") == current_sid:
            n += 1
    return n


def gate_wwa_provenance(project_dir: Path, current_sid: Optional[str]) -> GateResult:
    """The lead 'What Was Accomplished' must belong to the current session.

    Closes the bg /save handoff gap: a read-only / RCON / Bash-edit session that
    emits no Edit/Write events generates no WWA, so the curated merge leaves the
    PREVIOUS session's WWA as the lead — and ac_fresh (mtime-only) never notices.
    A cold-start then reads a handoff describing the wrong session.

    HARD-fails only when the session had real activity but the lead WWA is not
    stamped for it. A truly empty session (nothing to hand off) passes, and an
    unknown session id degrades to a warning rather than a block.
    """
    ac = project_dir / "Memory" / "activeContext.md"
    if not ac.exists():
        # ac_fresh already hard-fails on a missing file; don't double-report.
        return GateResult("ac_wwa_provenance", "pass", False, "activeContext.md missing (see ac_fresh)")
    lead = _lead_wwa_section(ac.read_text())
    if lead is None:
        return GateResult("ac_wwa_provenance", "pass", False,
                          "no 'What Was Accomplished' section present to verify")
    m = _WWA_SESSION_RE.search(lead)
    stamped = m.group(1) if m else None
    if current_sid and stamped == current_sid:
        return GateResult("ac_wwa_provenance", "pass", True,
                          f"lead WWA stamped current session {current_sid}")
    if not current_sid:
        return GateResult("ac_wwa_provenance", "warn", False,
                          "current session id unknown; cannot verify lead WWA provenance")
    activity = _session_event_count(project_dir, current_sid)
    if activity == 0:
        return GateResult("ac_wwa_provenance", "pass", False,
                          f"lead WWA not stamped for {current_sid}, but this session has no events "
                          f"(nothing to hand off)")
    where = f"belongs to session {stamped}" if stamped else "carries no session stamp"
    return GateResult("ac_wwa_provenance", "fail", True,
                      f"lead 'What Was Accomplished' {where}, not current session {current_sid} "
                      f"({activity} events this session) — the bg handoff gap: prepend a concrete "
                      f"current-session WWA stamped '<!-- wwa-session: {current_sid} -->' before committing")


# --------------------------------------------------------------------------- #
# Gate 4 — push durability (queue + backoff, never silent)
# --------------------------------------------------------------------------- #
def gate_push(project_dir: Path, dry_run: bool = False) -> GateResult:
    try:
        import push_retry
    except ImportError as exc:
        return GateResult("push_durability", "fail", True, f"push_retry unavailable: {exc}")
    if dry_run:
        has_dest, remote, _branch, _up = push_retry.push_destination(project_dir)
        queued = push_retry.status(project_dir).get("queued_total", 0)
        if has_dest:
            return GateResult("push_durability", "pass", True,
                              f"destination {remote} present; {queued} queued (dry-run, no push)")
        reason = "no_remote" if not remote else "no_upstream"
        return GateResult("push_durability", "pass", True,
                          f"no live push ({reason}); would queue HEAD; {queued} already queued (dry-run)")
    try:
        result = push_retry.ensure(project_dir, project_dir)
    except Exception as exc:
        return GateResult("push_durability", "fail", True, f"could not queue push: {exc}")
    st = result.get("status")
    if st == "pushed":
        return GateResult("push_durability", "pass", True, f"pushed to {result.get('remote')}")
    if st == "queued":
        return GateResult("push_durability", "pass", True,
                          f"no live push ({result.get('reason')}); HEAD queued, next retry {result.get('next_retry_after')} "
                          f"(total queued: {result.get('queued_total', '?')})")
    if st == "error" and result.get("reason") == "no_head":
        return GateResult("push_durability", "warn", False, "repo has no commits yet; nothing to push")
    return GateResult("push_durability", "fail", True, f"unexpected push result: {result}")


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def run_gates(project_dir: Path, current_sid: Optional[str], save_start: Optional[datetime],
              skip_push: bool = False, dry_run: bool = False) -> list[GateResult]:
    results: list[GateResult] = []
    results += gate_memory_substrate(project_dir)
    results.append(gate_session_integrity(project_dir, current_sid))
    results.append(gate_clobber(project_dir))
    results += gate_active_context(project_dir, save_start)
    results.append(gate_wwa_provenance(project_dir, current_sid))
    if not skip_push:
        results.append(gate_push(project_dir, dry_run=dry_run))
    return results


def _log(project_dir: Path, mode: str, current_sid: Optional[str], results: list[GateResult]) -> None:
    log_file = project_dir / "Memory" / "events" / "save-preflight.jsonl"
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        ts = _iso(_now())
        with open(log_file, "a") as f:
            for r in results:
                rec = {"ts": ts, "mode": mode, "session_id": current_sid, **asdict(r)}
                f.write(json.dumps(rec) + "\n")
    except OSError:
        pass  # logging must never block the save


def _table(results: list[GateResult]) -> str:
    icon = {"pass": "✓", "warn": "!", "fail": "✗"}
    lines = ["save pre-flight gates:"]
    for r in results:
        tag = " [HARD]" if (r.hard and r.status == "fail") else ""
        lines.append(f"  {icon.get(r.status, '?')} {r.gate:<18} {r.status}{tag}  {r.detail}")
    return "\n".join(lines)


def _hard_failures(results: list[GateResult]) -> list[GateResult]:
    return [r for r in results if r.status == "fail" and r.hard]


def _reason(failures: list[GateResult]) -> str:
    parts = "; ".join(f"[{r.gate}] {r.detail}" for r in failures)
    return (f"{parts}. Remediation: ensure ASHA_TRANSCRIPT_PATH points at THIS session's "
            f"transcript, re-run synthesis, regenerate the affected activeContext section, then finish.")


def _resolve_sid(args, transcript: Optional[Path]) -> Optional[str]:
    return (args.session_id or os.environ.get("CLAUDE_CODE_SESSION_ID")
            or (transcript.stem if transcript else None))


def main() -> int:
    ap = argparse.ArgumentParser(description="Pre-flight verification gate for /session:save")
    ap.add_argument("--mode", choices=["enforce", "guard", "report"], default="report")
    ap.add_argument("--project-dir", "-p", help="Project root (default $CLAUDE_PROJECT_DIR/cwd)")
    ap.add_argument("--transcript", "-t", help="Authoritative transcript path for this session")
    ap.add_argument("--session-id", "-s", help="Current session id")
    ap.add_argument("--save-start", help="ISO ts the save began (freshness check)")
    ap.add_argument("--skip-push", action="store_true", help="Skip the push gate (pre-commit guard)")
    args = ap.parse_args()

    env_pd = os.environ.get("CLAUDE_PROJECT_DIR")
    project_dir = Path(args.project_dir or env_pd or os.getcwd()).resolve()
    transcript = None
    tpath = args.transcript or os.environ.get("ASHA_TRANSCRIPT_PATH")
    if tpath:
        p = Path(os.path.expanduser(tpath))
        transcript = p if p.exists() else None
    current_sid = _resolve_sid(args, transcript)
    save_start = None
    if args.save_start:
        try:
            save_start = datetime.fromisoformat(args.save_start.replace("Z", "+00:00"))
        except ValueError:
            save_start = None

    results = run_gates(project_dir, current_sid, save_start,
                        skip_push=args.skip_push, dry_run=(args.mode == "report"))
    _log(project_dir, args.mode, current_sid, results)
    failures = _hard_failures(results)
    table = _table(results)

    if args.mode == "enforce":
        out = {"hard_fail": bool(failures), "reason": _reason(failures) if failures else "",
               "gates": [asdict(r) for r in results]}
        print(json.dumps(out))           # stdout -> consumed by Stop hook
        print(table, file=sys.stderr)    # stderr -> hook log / visibility
        return 0
    print(table)                          # guard/report -> human stdout
    if args.mode == "guard" and failures:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
