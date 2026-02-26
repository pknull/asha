#!/usr/bin/env python3
"""
Event Store - Structured event emission and synthesis for Asha Memory

Replaces markdown session logging (current-session.md) with structured events.
Events are stored in JSONL format for efficient append and query.

Usage:
    python event_store.py emit --type event --subtype file_modified --payload '{"file_path": "..."}'
    python event_store.py query --session-id abc123 --type event
    python event_store.py synthesize --output activeContext
    python event_store.py rotate --days 30
    python event_store.py stats
"""

import os
import sys
import json
import uuid
import fcntl
import argparse
import subprocess
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List
from collections import defaultdict


def detect_project_root() -> Path:
    """Find project root via environment, git, or upward search for Memory/"""
    # Layer 1: Use CLAUDE_PROJECT_DIR if set (hook invocation)
    claude_project_dir = os.environ.get("CLAUDE_PROJECT_DIR")
    if claude_project_dir:
        project_path = Path(claude_project_dir)
        if (project_path / "Memory").is_dir():
            return project_path

    # Layer 2: Try git root
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=True
        )
        git_root = Path(result.stdout.strip())
        if (git_root / "Memory").is_dir():
            return git_root
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass

    # Layer 3: Upward search for Memory/ directory
    search_dir = Path(__file__).parent.resolve()
    while search_dir != search_dir.parent:
        if (search_dir / "Memory").is_dir():
            return search_dir
        search_dir = search_dir.parent

    raise RuntimeError("Cannot detect project root. Ensure Memory/ directory exists.")


# Project paths (detected dynamically)
PROJECT_ROOT = detect_project_root()
EVENTS_DIR = PROJECT_ROOT / "Memory" / "events"
EVENTS_FILE = EVENTS_DIR / "events.jsonl"
ARCHIVE_DIR = EVENTS_DIR / "archive"

# Ensure events directory exists
EVENTS_DIR.mkdir(parents=True, exist_ok=True)


# Valid event types and subtypes
VALID_TYPES = {"task", "context", "event", "claim"}
VALID_SUBTYPES = {
    "task": {"created", "status_change", "completed", "blocked"},
    "context": {"decision", "reference", "config_change", "learning"},
    "event": {"file_modified", "file_created", "agent_deployed", "error", "command", "decision_point"}
}


def get_current_session_id() -> str:
    """Get session ID from marker or session file"""
    # Check marker file first
    marker = PROJECT_ROOT / "Work" / "markers" / "session-id"
    if marker.exists():
        return marker.read_text().strip()

    # Fall back to session file
    session_file = PROJECT_ROOT / "Memory" / "sessions" / "current-session.md"
    if session_file.exists():
        for line in session_file.read_text().split('\n'):
            if line.startswith('sessionID:'):
                return line.split(':', 1)[1].strip()

    # Generate new session ID
    return f"session_{datetime.now().strftime('%Y%m%d_%H%M%S')}"


def emit_event(
    event_type: str,
    subtype: str,
    payload: Dict[str, Any],
    source: str = "hook",
    tool_name: Optional[str] = None
) -> Dict:
    """Emit a structured event to the event store"""

    # Validate type
    if event_type not in VALID_TYPES:
        return {"error": f"Invalid type. Must be one of: {VALID_TYPES}"}

    # Validate subtype (warn but allow unknown)
    if subtype not in VALID_SUBTYPES.get(event_type, set()):
        # Allow unknown subtypes but log warning
        pass

    event_id = f"evt_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
    session_id = get_current_session_id()

    event = {
        "id": event_id,
        "timestamp": datetime.now(tz=None).isoformat() + "Z",
        "session_id": session_id,
        "type": event_type,
        "subtype": subtype,
        "payload": payload,
        "metadata": {
            "source": source,
            "tool_name": tool_name,
            "project_dir": str(PROJECT_ROOT)
        }
    }

    # Append to JSONL file with exclusive lock
    with open(EVENTS_FILE, 'a') as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.write(json.dumps(event, ensure_ascii=False) + '\n')
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)

    return {"status": "emitted", "event_id": event_id, "session_id": session_id}


def query_events(
    session_id: Optional[str] = None,
    event_type: Optional[str] = None,
    subtype: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    limit: int = 100
) -> Dict:
    """Query events from the store with filters"""

    if not EVENTS_FILE.exists():
        return {"events": [], "count": 0, "total_scanned": 0}

    events = []
    scanned = 0

    with open(EVENTS_FILE, 'r') as f:
        for line in f:
            if not line.strip():
                continue
            scanned += 1

            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            # Apply filters
            if session_id and event.get("session_id") != session_id:
                continue
            if event_type and event.get("type") != event_type:
                continue
            if subtype and event.get("subtype") != subtype:
                continue

            if since:
                event_time = event.get("timestamp", "")
                if event_time < since:
                    continue

            if until:
                event_time = event.get("timestamp", "")
                if event_time > until:
                    continue

            events.append(event)

    # Sort by timestamp descending (most recent first)
    events.sort(key=lambda e: e.get("timestamp", ""), reverse=True)

    return {
        "events": events[:limit],
        "count": min(len(events), limit),
        "total_matched": len(events),
        "total_scanned": scanned
    }


# =============================================================================
# File Claims - Dynamic file locking for agent coordination
# =============================================================================

def claim_file(
    file_path: str,
    agent: str,
    reason: Optional[str] = None
) -> dict:
    """
    Claim a file for exclusive work. Other agents should check claims before editing.

    Claims are soft locks - advisory, not enforced. jj handles actual conflicts.
    """
    return emit_event(
        event_type="claim",
        subtype="acquire",
        payload={
            "file_path": file_path,
            "agent": agent,
            "reason": reason or "Working on file"
        }
    )


def release_file(
    file_path: str,
    agent: str
) -> dict:
    """Release a claimed file."""
    return emit_event(
        event_type="claim",
        subtype="release",
        payload={
            "file_path": file_path,
            "agent": agent
        }
    )


def check_claims(
    file_path: Optional[str] = None
) -> dict:
    """
    Check active file claims.

    Returns claims that have acquire but no matching release.
    """
    # Get all claim events from current session
    result = query_events(event_type="claim", limit=500)

    claims: Dict[str, dict] = {}  # file_path -> claim info

    for event in reversed(result["events"]):  # Process oldest first
        path = event["payload"].get("file_path")
        if not path:
            continue

        if event["subtype"] == "acquire":
            claims[path] = {
                "file_path": path,
                "agent": event["payload"].get("agent"),
                "reason": event["payload"].get("reason"),
                "claimed_at": event["timestamp"],
                "event_id": event["id"]
            }
        elif event["subtype"] == "release":
            # Release clears the claim
            if path in claims:
                del claims[path]

    # Filter to specific file if requested
    if file_path:
        if file_path in claims:
            return {"claims": [claims[file_path]], "count": 1}
        return {"claims": [], "count": 0}

    return {
        "claims": list(claims.values()),
        "count": len(claims)
    }


def synthesize_active_context(
    session_id: Optional[str] = None,
    days: int = 7
) -> str:
    """Synthesize activeContext.md content from recent events"""

    # Query recent events
    if session_id:
        result = query_events(session_id=session_id, limit=1000)
    else:
        since = (datetime.now(tz=None) - timedelta(days=days)).isoformat() + "Z"
        result = query_events(since=since, limit=1000)

    events = result["events"]

    if not events:
        return "# activeContext\n\nNo recent events found.\n"

    # Separate by type
    tasks = [e for e in events if e["type"] == "task"]
    contexts = [e for e in events if e["type"] == "context"]
    activities = [e for e in events if e["type"] == "event"]

    lines = []
    lines.append("---")
    lines.append(f'version: "1.0"')
    lines.append(f'lastUpdated: "{datetime.now().strftime("%Y-%m-%d")}"')
    lines.append(f'lifecycle: "active"')
    lines.append(f'synthesizedFrom: "events"')
    lines.append("---")
    lines.append("")
    lines.append("# activeContext")
    lines.append("")

    # Current Project Status (from recent decisions)
    decisions = [c for c in contexts if c["subtype"] == "decision"]
    learnings = [c for c in contexts if c["subtype"] == "learning"]

    if decisions or learnings:
        lines.append("## Current Project Status")
        lines.append("")

        if decisions:
            for d in decisions[-5:]:
                key = d["payload"].get("key", "Decision")
                value = d["payload"].get("value", "")
                lines.append(f"- **{key}**: {value}")

        if learnings:
            lines.append("")
            lines.append("**Key Learnings**:")
            for l in learnings[-5:]:
                insight = l["payload"].get("insight", l["payload"].get("detail", ""))
                lines.append(f"- {insight}")

        lines.append("")

    # Recent Activities (grouped by date)
    if activities:
        by_date: Dict[str, List] = defaultdict(list)
        for a in activities:
            date = a["timestamp"][:10]
            by_date[date].append(a)

        lines.append("## Recent Activities")
        lines.append("")

        # Last 7 days
        for date in sorted(by_date.keys(), reverse=True)[:7]:
            lines.append(f"**{date}**:")
            lines.append("")

            # Group by subtype within date
            day_events = by_date[date]
            for evt in day_events[:15]:  # Max 15 per day
                subtype = evt["subtype"]
                payload = evt["payload"]

                if subtype == "file_modified" or subtype == "file_created":
                    file_path = payload.get("file_path", payload.get("detail", "unknown"))
                    lines.append(f"- [{subtype}] {file_path}")
                elif subtype == "agent_deployed":
                    agent = payload.get("agent_type", "unknown")
                    desc = payload.get("description", "")
                    lines.append(f"- [agent] {agent}: {desc}")
                elif subtype == "error":
                    error = payload.get("error", payload.get("detail", "unknown"))
                    lines.append(f"- [error] {error}")
                elif subtype == "decision_point":
                    decision = payload.get("detail", payload.get("questions", ""))
                    lines.append(f"- [decision] {decision}")
                else:
                    detail = payload.get("detail", str(payload)[:100])
                    lines.append(f"- [{subtype}] {detail}")

            lines.append("")

    # Next Steps (from pending tasks)
    pending = [t for t in tasks if t["payload"].get("status") == "pending"]
    in_progress = [t for t in tasks if t["payload"].get("status") == "in_progress"]
    blocked = [t for t in tasks if t["payload"].get("status") == "blocked"]

    if pending or in_progress or blocked:
        lines.append("## Next Steps")
        lines.append("")

        if in_progress:
            lines.append("**In Progress**:")
            for t in in_progress[-5:]:
                subject = t["payload"].get("subject", "Unknown task")
                lines.append(f"- {subject}")
            lines.append("")

        if pending:
            lines.append("**Pending**:")
            for t in pending[-10:]:
                subject = t["payload"].get("subject", "Unknown task")
                lines.append(f"- [ ] {subject}")
            lines.append("")

        if blocked:
            lines.append("**Blocked**:")
            for t in blocked[-5:]:
                subject = t["payload"].get("subject", "Unknown task")
                reason = t["payload"].get("context", "")
                lines.append(f"- {subject}" + (f" ({reason})" if reason else ""))
            lines.append("")

    return "\n".join(lines)


def rotate_events(days_threshold: int = 30) -> Dict:
    """Archive events older than threshold to monthly files"""

    if not EVENTS_FILE.exists():
        return {"archived": 0, "retained": 0}

    ARCHIVE_DIR.mkdir(exist_ok=True)

    cutoff = datetime.now(tz=None) - timedelta(days=days_threshold)
    cutoff_str = cutoff.isoformat() + "Z"

    current_events = []
    archived_count = 0
    archive_files: Dict[str, List] = {}

    with open(EVENTS_FILE, 'r') as f:
        for line in f:
            if not line.strip():
                continue

            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            event_time = event.get("timestamp", "")

            if event_time < cutoff_str:
                # Archive by month
                month_key = event_time[:7]  # YYYY-MM
                if month_key not in archive_files:
                    archive_files[month_key] = []
                archive_files[month_key].append(event)
                archived_count += 1
            else:
                current_events.append(event)

    # Append to archive files
    for month_key, events in archive_files.items():
        archive_path = ARCHIVE_DIR / f"events-{month_key}.jsonl"
        with open(archive_path, 'a') as f:
            for event in events:
                f.write(json.dumps(event, ensure_ascii=False) + '\n')

    # Rewrite current events file
    with open(EVENTS_FILE, 'w') as f:
        for event in current_events:
            f.write(json.dumps(event, ensure_ascii=False) + '\n')

    return {
        "archived": archived_count,
        "retained": len(current_events),
        "archive_files": list(archive_files.keys())
    }


def get_stats() -> Dict:
    """Get event store statistics"""

    stats = {
        "events_file": str(EVENTS_FILE),
        "total_events": 0,
        "by_type": defaultdict(int),
        "by_subtype": defaultdict(int),
        "sessions": set(),
        "date_range": {"earliest": None, "latest": None},
        "archive_files": []
    }

    if EVENTS_FILE.exists():
        with open(EVENTS_FILE, 'r') as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                    stats["total_events"] += 1
                    stats["by_type"][event.get("type", "unknown")] += 1
                    stats["by_subtype"][event.get("subtype", "unknown")] += 1
                    stats["sessions"].add(event.get("session_id", ""))

                    ts = event.get("timestamp", "")
                    if ts:
                        if stats["date_range"]["earliest"] is None or ts < stats["date_range"]["earliest"]:
                            stats["date_range"]["earliest"] = ts
                        if stats["date_range"]["latest"] is None or ts > stats["date_range"]["latest"]:
                            stats["date_range"]["latest"] = ts
                except json.JSONDecodeError:
                    pass

    # Check archives
    if ARCHIVE_DIR.exists():
        stats["archive_files"] = sorted([f.name for f in ARCHIVE_DIR.glob("events-*.jsonl")])

    # Convert sets to counts for JSON serialization
    stats["session_count"] = len(stats["sessions"])
    del stats["sessions"]
    stats["by_type"] = dict(stats["by_type"])
    stats["by_subtype"] = dict(stats["by_subtype"])

    return stats


# =============================================================================
# CLI Interface
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Event Store - Structured event emission and synthesis",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s emit --type event --subtype file_modified --payload '{"file_path": "Memory/activeContext.md"}'
  %(prog)s query --type event --limit 20
  %(prog)s query --session-id clever-skipping --type task
  %(prog)s synthesize --days 7
  %(prog)s rotate --days 30
  %(prog)s stats
"""
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # Emit command
    emit_parser = subparsers.add_parser("emit", help="Emit a new event")
    emit_parser.add_argument("--type", "-t", required=True, choices=list(VALID_TYPES), help="Event type")
    emit_parser.add_argument("--subtype", "-s", required=True, help="Event subtype")
    emit_parser.add_argument("--payload", "-p", required=True, help="JSON payload")
    emit_parser.add_argument("--source", default="cli", help="Event source (default: cli)")
    emit_parser.add_argument("--tool", help="Tool name that triggered the event")

    # Query command
    query_parser = subparsers.add_parser("query", help="Query events")
    query_parser.add_argument("--session-id", "-S", help="Filter by session ID")
    query_parser.add_argument("--type", "-t", choices=list(VALID_TYPES), help="Filter by type")
    query_parser.add_argument("--subtype", "-s", help="Filter by subtype")
    query_parser.add_argument("--since", help="Events after this timestamp (ISO8601)")
    query_parser.add_argument("--until", help="Events before this timestamp (ISO8601)")
    query_parser.add_argument("--limit", "-n", type=int, default=100, help="Max results (default: 100)")

    # Synthesize command
    synth_parser = subparsers.add_parser("synthesize", help="Synthesize activeContext from events")
    synth_parser.add_argument("--session-id", "-S", help="Synthesize for specific session")
    synth_parser.add_argument("--days", "-d", type=int, default=7, help="Days of history (default: 7)")
    synth_parser.add_argument("--output", "-o", help="Output file (default: stdout)")

    # Rotate command
    rotate_parser = subparsers.add_parser("rotate", help="Archive old events")
    rotate_parser.add_argument("--days", "-d", type=int, default=30, help="Archive events older than N days (default: 30)")

    # Stats command
    subparsers.add_parser("stats", help="Show event store statistics")

    # Claim command
    claim_parser = subparsers.add_parser("claim", help="Claim a file for exclusive work")
    claim_parser.add_argument("file_path", help="Path to file to claim")
    claim_parser.add_argument("--agent", "-a", required=True, help="Agent name claiming the file")
    claim_parser.add_argument("--reason", "-r", help="Reason for claim")

    # Release command
    release_parser = subparsers.add_parser("release", help="Release a claimed file")
    release_parser.add_argument("file_path", help="Path to file to release")
    release_parser.add_argument("--agent", "-a", required=True, help="Agent name releasing the file")

    # Check-claims command
    claims_parser = subparsers.add_parser("claims", help="Check active file claims")
    claims_parser.add_argument("--file", "-f", help="Check specific file")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    try:
        if args.command == "emit":
            try:
                payload = json.loads(args.payload)
            except json.JSONDecodeError as e:
                print(json.dumps({"error": f"Invalid JSON payload: {e}"}))
                sys.exit(1)

            result = emit_event(
                event_type=args.type,
                subtype=args.subtype,
                payload=payload,
                source=args.source,
                tool_name=args.tool
            )
            print(json.dumps(result, indent=2))

        elif args.command == "query":
            result = query_events(
                session_id=args.session_id,
                event_type=args.type,
                subtype=args.subtype,
                since=args.since,
                until=args.until,
                limit=args.limit
            )
            print(json.dumps(result, indent=2))

        elif args.command == "synthesize":
            content = synthesize_active_context(
                session_id=args.session_id,
                days=args.days
            )
            if args.output:
                Path(args.output).write_text(content)
                print(json.dumps({"status": "written", "path": args.output}))
            else:
                print(content)

        elif args.command == "rotate":
            result = rotate_events(days_threshold=args.days)
            print(json.dumps(result, indent=2))

        elif args.command == "stats":
            result = get_stats()
            print(json.dumps(result, indent=2))

        elif args.command == "claim":
            result = claim_file(
                file_path=args.file_path,
                agent=args.agent,
                reason=args.reason
            )
            print(json.dumps(result, indent=2))

        elif args.command == "release":
            result = release_file(
                file_path=args.file_path,
                agent=args.agent
            )
            print(json.dumps(result, indent=2))

        elif args.command == "claims":
            result = check_claims(file_path=args.file)
            print(json.dumps(result, indent=2))

    except Exception as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
