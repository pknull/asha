#!/usr/bin/env python3
"""
Facet Ingest - Auto-ingest Claude Code session facets into ReasoningBank

Reads structured session telemetry from ~/.claude/usage-data/facets/,
maps to current project via ~/.claude/history.jsonl, and records
patterns in the ReasoningBank for cross-session learning.

Usage:
    python facet_ingest.py ingest   # Process unprocessed facets for current project
    python facet_ingest.py status   # Show facet counts (total, ingested, pending)
"""

import json
import sqlite3
import sys
from pathlib import Path

# Import from sibling module
sys.path.insert(0, str(Path(__file__).parent))
from reasoning_bank import detect_project_root, init_db, record_pattern

FACETS_DIR = Path.home() / ".claude" / "usage-data" / "facets"
HISTORY_FILE = Path.home() / ".claude" / "history.jsonl"

OUTCOME_SCORES = {
    "fully_achieved": 0.95,
    "mostly_achieved": 0.75,
    "partially_achieved": 0.45,
    "not_achieved": 0.15,
}

# Minimum score for primary_success patterns (only applied when outcome >= mostly_achieved)
PRIMARY_SUCCESS_FLOOR = 0.8


def _init_db_with_timeout() -> sqlite3.Connection:
    """Wrapper around init_db that adds busy_timeout and WAL mode."""
    conn = init_db()
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def ensure_ingested_table(conn):
    """Create ingested_facets tracking table if needed."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ingested_facets (
            session_id TEXT PRIMARY KEY,
            ingested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            patterns_created INTEGER DEFAULT 0,
            project_path TEXT NOT NULL
        )
    """)
    conn.commit()


def build_session_project_map():
    """Map session_id -> project_path from history.jsonl."""
    if not HISTORY_FILE.exists():
        return {}

    mapping = {}
    try:
        with open(HISTORY_FILE) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    sid = entry.get("sessionId", "")
                    project = entry.get("project", "")
                    if sid and project:
                        mapping[sid] = project
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass

    return mapping


def load_facets():
    """Load all facet files, returning list of (session_id, facet_dict)."""
    if not FACETS_DIR.is_dir():
        return []

    facets = []
    for fpath in FACETS_DIR.glob("*.json"):
        try:
            with open(fpath) as f:
                data = json.loads(f.read())
            sid = data.get("session_id", fpath.stem)
            facets.append((sid, data))
        except (json.JSONDecodeError, OSError) as e:
            print(f"Warning: skipping malformed facet {fpath.name}: {e}", file=sys.stderr)
    return facets


def compute_score(facet):
    """Derive success score from outcome + satisfaction signals."""
    outcome = facet.get("outcome", "partially_achieved")
    base = OUTCOME_SCORES.get(outcome, 0.5)

    sat_counts = facet.get("user_satisfaction_counts", {})
    satisfied = sat_counts.get("satisfied", 0) + sat_counts.get("likely_satisfied", 0)
    dissatisfied = sat_counts.get("dissatisfied", 0) + sat_counts.get("likely_dissatisfied", 0)
    total = satisfied + dissatisfied

    if total > 0:
        ratio = satisfied / total
        modifier = (ratio - 0.5) * 0.2
    else:
        modifier = 0.0

    return max(0.0, min(1.0, base + modifier))


def ingest_facet(facet):
    """Convert a single facet into ReasoningBank patterns. Returns count created."""
    count = 0
    score = compute_score(facet)
    session_type = facet.get("session_type", "unknown")
    outcome = facet.get("outcome", "unknown")
    summary = facet.get("brief_summary", "")
    goal = facet.get("underlying_goal", "")

    # Pattern 1: Session workflow (always)
    action = f"Session({session_type}): {goal}" if goal else f"Session({session_type})"
    record_pattern(
        pattern_type="workflow",
        context=summary or "brief_summary",
        action=action,
        outcome=outcome,
        score=score,
        tags=["facet", "auto-ingest", session_type],
    )
    count += 1

    # Pattern 2: Friction detail (if present)
    friction = facet.get("friction_counts", {})
    if friction:
        numeric_friction = {k: v for k, v in friction.items() if isinstance(v, (int, float)) and v > 0}
        parts = [f"{ftype}({n})" for ftype, n in numeric_friction.items()]
        if parts:
            total_friction = sum(numeric_friction.values())
            friction_score = max(0.1, 1.0 - total_friction * 0.1)
            record_pattern(
                pattern_type="workflow",
                context=facet.get("friction_detail", "friction_detail"),
                action=f"Friction: {', '.join(parts)}",
                outcome=str(total_friction),
                score=friction_score,
                tags=["facet", "friction", "auto-ingest"],
            )
            count += 1

    # Pattern 3: Primary success (if present, only boosted for successful outcomes)
    primary_success = facet.get("primary_success", "")
    if primary_success:
        success_score = max(PRIMARY_SUCCESS_FLOOR, score) if score >= 0.45 else score
        record_pattern(
            pattern_type="workflow",
            context=summary or "brief_summary",
            action=f"Success: {primary_success}",
            outcome=outcome,
            score=success_score,
            tags=["facet", "success", "auto-ingest"],
        )
        count += 1

    return count


def cmd_ingest():
    """Process unprocessed facets for current project."""
    try:
        project_root = detect_project_root()
    except RuntimeError:
        print(json.dumps({"ingested": 0, "error": "no_project_root"}))
        return

    project_path = str(project_root)
    session_map = build_session_project_map()
    facets = load_facets()

    if not facets:
        print(json.dumps({"ingested": 0, "skipped": 0, "reason": "no_facets"}))
        return

    # Read already-ingested set, then close connection
    # (record_pattern opens its own connection, so we can't hold one during ingestion)
    conn = _init_db_with_timeout()
    ensure_ingested_table(conn)
    cursor = conn.execute(
        "SELECT session_id FROM ingested_facets WHERE project_path = ?",
        (project_path,),
    )
    already_ingested = {row[0] for row in cursor.fetchall()}
    conn.close()

    # Filter to eligible facets
    to_ingest = []
    skipped_count = 0
    for session_id, facet in facets:
        if session_id in already_ingested:
            skipped_count += 1
            continue
        facet_project = session_map.get(session_id, "")
        if facet_project != project_path:
            skipped_count += 1
            continue
        to_ingest.append((session_id, facet))

    # Ingest each facet (record_pattern manages its own connection)
    ingested_count = 0
    for session_id, facet in to_ingest:
        patterns_created = ingest_facet(facet)

        # Track ingestion in a fresh connection
        conn = _init_db_with_timeout()
        conn.execute(
            "INSERT OR IGNORE INTO ingested_facets (session_id, patterns_created, project_path) VALUES (?, ?, ?)",
            (session_id, patterns_created, project_path),
        )
        conn.commit()
        conn.close()
        ingested_count += 1

    print(json.dumps({
        "ingested": ingested_count,
        "skipped": skipped_count,
        "project": project_path,
    }))


def cmd_status():
    """Show facet ingestion status."""
    try:
        project_root = detect_project_root()
    except RuntimeError:
        print(json.dumps({"error": "no_project_root"}))
        return

    project_path = str(project_root)
    session_map = build_session_project_map()
    facets = load_facets()

    # Count facets belonging to this project
    project_facets = 0
    for session_id, _ in facets:
        if session_map.get(session_id, "") == project_path:
            project_facets += 1

    # Count already ingested
    ingested_count = 0
    try:
        conn = _init_db_with_timeout()
        ensure_ingested_table(conn)
        cursor = conn.execute(
            "SELECT COUNT(*) FROM ingested_facets WHERE project_path = ?",
            (project_path,),
        )
        ingested_count = cursor.fetchone()[0]
        conn.close()
    except Exception:
        pass

    print(json.dumps({
        "total_facets": len(facets),
        "project_facets": project_facets,
        "ingested": ingested_count,
        "pending": max(0, project_facets - ingested_count),
        "project": project_path,
    }))


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("ingest", "status"):
        print("Usage: facet_ingest.py {ingest|status}", file=sys.stderr)
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "ingest":
        cmd_ingest()
    elif cmd == "status":
        cmd_status()


if __name__ == "__main__":
    main()
