#!/usr/bin/env python3
"""
Reasoning Bank - Pattern tracking for learned strategies

Records what worked/failed during sessions, enabling retrieval of
historically successful approaches for similar situations.

Usage:
    python reasoning_bank.py record --type workflow --context "..." --action "..." --score 0.9
    python reasoning_bank.py query --type code --context "refactoring"
    python reasoning_bank.py error --type ImportError --signature "No module named 'x'" --resolution "pip install x"
    python reasoning_bank.py tool --name grep --use-case "finding definitions" --success
    python reasoning_bank.py stats
    python reasoning_bank.py export --format json
"""

import sys
import json
import sqlite3
import argparse
import subprocess
from pathlib import Path
from datetime import datetime
from typing import Optional


def detect_project_root() -> Path:
    """Find project root via git or upward search for Memory/"""
    # Try git root first
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

    # Upward search for Memory/ directory
    search_dir = Path(__file__).parent.resolve()
    while search_dir != search_dir.parent:
        if (search_dir / "Memory").is_dir():
            return search_dir
        search_dir = search_dir.parent

    raise RuntimeError("Cannot detect project root. Ensure Memory/ directory exists.")


# Project paths (detected dynamically)
PROJECT_ROOT = detect_project_root()
REASONING_BANK_DIR = PROJECT_ROOT / "Memory" / "reasoning_bank"
DB_PATH = REASONING_BANK_DIR / "patterns.db"

# Pattern types
VALID_PATTERN_TYPES = {"code", "error", "workflow", "tool_use", "decision"}


def init_db() -> sqlite3.Connection:
    """Initialize database with schema if needed"""
    REASONING_BANK_DIR.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # Enable foreign keys
    conn.execute("PRAGMA foreign_keys = ON")

    # Create tables
    conn.executescript("""
        -- Core pattern storage
        CREATE TABLE IF NOT EXISTS patterns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pattern_type TEXT NOT NULL CHECK(pattern_type IN ('code', 'error', 'workflow', 'tool_use', 'decision')),
            context TEXT NOT NULL,
            action TEXT NOT NULL,
            outcome TEXT,
            success_score REAL DEFAULT 0.5 CHECK(success_score >= 0.0 AND success_score <= 1.0),
            feedback TEXT,
            session_id TEXT,
            tags TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        -- Error resolution tracking
        CREATE TABLE IF NOT EXISTS error_resolutions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            error_type TEXT NOT NULL,
            error_signature TEXT NOT NULL,
            resolution TEXT NOT NULL,
            prevention_hint TEXT,
            occurrence_count INTEGER DEFAULT 1,
            last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(error_type, error_signature)
        );

        -- Tool effectiveness tracking
        CREATE TABLE IF NOT EXISTS tool_effectiveness (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tool_name TEXT NOT NULL,
            use_case TEXT NOT NULL,
            success_count INTEGER DEFAULT 0,
            failure_count INTEGER DEFAULT 0,
            avg_duration_ms REAL,
            notes TEXT,
            last_used TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(tool_name, use_case)
        );

        -- Indexes for common queries
        CREATE INDEX IF NOT EXISTS idx_patterns_type ON patterns(pattern_type);
        CREATE INDEX IF NOT EXISTS idx_patterns_score ON patterns(success_score DESC);
        CREATE INDEX IF NOT EXISTS idx_patterns_created ON patterns(created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_errors_type ON error_resolutions(error_type);
        CREATE INDEX IF NOT EXISTS idx_tools_name ON tool_effectiveness(tool_name);
    """)

    conn.commit()
    return conn


def get_current_session_id() -> Optional[str]:
    """Read current session ID from marker file"""
    marker_file = PROJECT_ROOT / "Work" / "markers" / "session-id"
    if marker_file.exists():
        return marker_file.read_text().strip()
    # Fallback: check Memory/sessions/current-session.md frontmatter
    session_file = PROJECT_ROOT / "Memory" / "sessions" / "current-session.md"
    if session_file.exists():
        content = session_file.read_text()
        for line in content.split('\n'):
            if line.startswith('sessionID:'):
                return line.split(':', 1)[1].strip()
    return None


# =============================================================================
# Record Operations
# =============================================================================

def record_pattern(
    pattern_type: str,
    context: str,
    action: str,
    outcome: Optional[str] = None,
    score: float = 0.5,
    feedback: Optional[str] = None,
    tags: Optional[list[str]] = None
) -> dict:
    """Record a new pattern"""
    if pattern_type not in VALID_PATTERN_TYPES:
        return {"error": f"Invalid pattern type. Must be one of: {VALID_PATTERN_TYPES}"}

    if not 0.0 <= score <= 1.0:
        return {"error": "Score must be between 0.0 and 1.0"}

    conn = init_db()
    cursor = conn.cursor()

    session_id = get_current_session_id()
    tags_str = ",".join(tags) if tags else None

    cursor.execute("""
        INSERT INTO patterns (pattern_type, context, action, outcome, success_score, feedback, session_id, tags)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (pattern_type, context, action, outcome, score, feedback, session_id, tags_str))

    pattern_id = cursor.lastrowid
    conn.commit()
    conn.close()

    return {
        "status": "recorded",
        "id": pattern_id,
        "pattern_type": pattern_type,
        "success_score": score,
        "session_id": session_id
    }


def record_error_resolution(
    error_type: str,
    signature: str,
    resolution: str,
    prevention: Optional[str] = None
) -> dict:
    """Record or update an error resolution"""
    conn = init_db()
    cursor = conn.cursor()

    # Try to update existing, insert if not found
    cursor.execute("""
        INSERT INTO error_resolutions (error_type, error_signature, resolution, prevention_hint)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(error_type, error_signature) DO UPDATE SET
            resolution = excluded.resolution,
            prevention_hint = COALESCE(excluded.prevention_hint, prevention_hint),
            occurrence_count = occurrence_count + 1,
            last_seen = CURRENT_TIMESTAMP
    """, (error_type, signature, resolution, prevention))

    # Get the record
    cursor.execute("""
        SELECT id, occurrence_count FROM error_resolutions
        WHERE error_type = ? AND error_signature = ?
    """, (error_type, signature))
    row = cursor.fetchone()

    conn.commit()
    conn.close()

    return {
        "status": "recorded",
        "id": row["id"],
        "error_type": error_type,
        "occurrence_count": row["occurrence_count"]
    }


def record_tool_usage(
    tool_name: str,
    use_case: str,
    success: bool,
    duration_ms: Optional[float] = None,
    notes: Optional[str] = None
) -> dict:
    """Record tool usage outcome"""
    conn = init_db()
    cursor = conn.cursor()

    # Get existing record if any
    cursor.execute("""
        SELECT id, success_count, failure_count, avg_duration_ms
        FROM tool_effectiveness
        WHERE tool_name = ? AND use_case = ?
    """, (tool_name, use_case))
    existing = cursor.fetchone()

    if existing:
        # Update existing
        success_count = existing["success_count"] + (1 if success else 0)
        failure_count = existing["failure_count"] + (0 if success else 1)

        # Update rolling average duration
        if duration_ms is not None:
            old_avg = existing["avg_duration_ms"] or 0
            total_count = success_count + failure_count
            new_avg = ((old_avg * (total_count - 1)) + duration_ms) / total_count
        else:
            new_avg = existing["avg_duration_ms"]

        cursor.execute("""
            UPDATE tool_effectiveness SET
                success_count = ?,
                failure_count = ?,
                avg_duration_ms = ?,
                notes = COALESCE(?, notes),
                last_used = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (success_count, failure_count, new_avg, notes, existing["id"]))
        record_id = existing["id"]
    else:
        # Insert new
        cursor.execute("""
            INSERT INTO tool_effectiveness (tool_name, use_case, success_count, failure_count, avg_duration_ms, notes)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (tool_name, use_case, 1 if success else 0, 0 if success else 1, duration_ms, notes))
        record_id = cursor.lastrowid

    conn.commit()
    conn.close()

    return {
        "status": "recorded",
        "id": record_id,
        "tool_name": tool_name,
        "use_case": use_case,
        "success": success
    }


# =============================================================================
# Query Operations
# =============================================================================

def query_patterns(
    pattern_type: Optional[str] = None,
    context: Optional[str] = None,
    min_score: float = 0.0,
    limit: int = 10
) -> dict:
    """Query patterns by type and context similarity"""
    conn = init_db()
    cursor = conn.cursor()

    conditions = ["success_score >= ?"]
    params: list = [min_score]

    if pattern_type:
        conditions.append("pattern_type = ?")
        params.append(pattern_type)

    if context:
        # Simple keyword matching (could be enhanced with FTS5)
        keywords = context.lower().split()
        for kw in keywords[:5]:  # Limit to 5 keywords
            conditions.append("LOWER(context) LIKE ?")
            params.append(f"%{kw}%")

    query = f"""
        SELECT id, pattern_type, context, action, outcome, success_score, feedback, session_id, tags, created_at
        FROM patterns
        WHERE {' AND '.join(conditions)}
        ORDER BY success_score DESC, created_at DESC
        LIMIT ?
    """
    params.append(limit)

    cursor.execute(query, params)
    rows = cursor.fetchall()
    conn.close()

    results = []
    for row in rows:
        results.append({
            "id": row["id"],
            "pattern_type": row["pattern_type"],
            "context": row["context"],
            "action": row["action"],
            "outcome": row["outcome"],
            "success_score": row["success_score"],
            "feedback": row["feedback"],
            "session_id": row["session_id"],
            "tags": row["tags"].split(",") if row["tags"] else [],
            "created_at": row["created_at"]
        })

    return {"results": results, "count": len(results)}


def query_error_resolution(error_type: str, signature: Optional[str] = None) -> dict:
    """Look up error resolutions"""
    conn = init_db()
    cursor = conn.cursor()

    if signature:
        # Exact match first, then partial
        cursor.execute("""
            SELECT * FROM error_resolutions
            WHERE error_type = ? AND error_signature = ?
        """, (error_type, signature))
        row = cursor.fetchone()

        if not row:
            # Partial match
            cursor.execute("""
                SELECT * FROM error_resolutions
                WHERE error_type = ? AND ? LIKE '%' || error_signature || '%'
                ORDER BY occurrence_count DESC
                LIMIT 1
            """, (error_type, signature))
            row = cursor.fetchone()
    else:
        # Get all for this error type
        cursor.execute("""
            SELECT * FROM error_resolutions
            WHERE error_type = ?
            ORDER BY occurrence_count DESC
        """, (error_type,))
        rows = cursor.fetchall()
        conn.close()
        return {
            "results": [dict(row) for row in rows],
            "count": len(rows)
        }

    conn.close()

    if row:
        return {"found": True, "resolution": dict(row)}
    return {"found": False, "error_type": error_type}


def query_tool_effectiveness(tool_name: Optional[str] = None) -> dict:
    """Query tool effectiveness statistics"""
    conn = init_db()
    cursor = conn.cursor()

    if tool_name:
        cursor.execute("""
            SELECT *,
                   CASE WHEN (success_count + failure_count) > 0
                        THEN CAST(success_count AS REAL) / (success_count + failure_count)
                        ELSE 0 END as success_rate
            FROM tool_effectiveness
            WHERE tool_name = ?
            ORDER BY success_rate DESC
        """, (tool_name,))
    else:
        cursor.execute("""
            SELECT *,
                   CASE WHEN (success_count + failure_count) > 0
                        THEN CAST(success_count AS REAL) / (success_count + failure_count)
                        ELSE 0 END as success_rate
            FROM tool_effectiveness
            ORDER BY success_rate DESC, (success_count + failure_count) DESC
        """)

    rows = cursor.fetchall()
    conn.close()

    results = []
    for row in rows:
        results.append({
            "tool_name": row["tool_name"],
            "use_case": row["use_case"],
            "success_count": row["success_count"],
            "failure_count": row["failure_count"],
            "success_rate": round(row["success_rate"], 3),
            "avg_duration_ms": row["avg_duration_ms"],
            "notes": row["notes"],
            "last_used": row["last_used"]
        })

    return {"results": results, "count": len(results)}


# =============================================================================
# Feedback Operations
# =============================================================================

def update_pattern_score(pattern_id: int, score: float, feedback: Optional[str] = None) -> dict:
    """Update a pattern's success score based on feedback"""
    if not 0.0 <= score <= 1.0:
        return {"error": "Score must be between 0.0 and 1.0"}

    conn = init_db()
    cursor = conn.cursor()

    if feedback:
        cursor.execute("""
            UPDATE patterns SET
                success_score = ?,
                feedback = COALESCE(feedback || ' | ', '') || ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (score, feedback, pattern_id))
    else:
        cursor.execute("""
            UPDATE patterns SET
                success_score = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (score, pattern_id))

    affected = cursor.rowcount
    conn.commit()
    conn.close()

    if affected:
        return {"status": "updated", "id": pattern_id, "new_score": score}
    return {"error": f"Pattern {pattern_id} not found"}


# =============================================================================
# Statistics & Export
# =============================================================================

def get_stats() -> dict:
    """Get database statistics"""
    conn = init_db()
    cursor = conn.cursor()

    # Pattern stats
    cursor.execute("SELECT COUNT(*) as total, AVG(success_score) as avg_score FROM patterns")
    pattern_stats = cursor.fetchone()

    cursor.execute("""
        SELECT pattern_type, COUNT(*) as count, AVG(success_score) as avg_score
        FROM patterns GROUP BY pattern_type ORDER BY count DESC
    """)
    pattern_types = [dict(row) for row in cursor.fetchall()]

    # Error stats
    cursor.execute("SELECT COUNT(*) as total, SUM(occurrence_count) as total_occurrences FROM error_resolutions")
    error_stats = cursor.fetchone()

    # Tool stats
    cursor.execute("""
        SELECT COUNT(*) as total,
               SUM(success_count) as total_successes,
               SUM(failure_count) as total_failures
        FROM tool_effectiveness
    """)
    tool_stats = cursor.fetchone()

    # Recent activity
    cursor.execute("""
        SELECT created_at, pattern_type, context FROM patterns
        ORDER BY created_at DESC LIMIT 5
    """)
    recent = [dict(row) for row in cursor.fetchall()]

    conn.close()

    return {
        "patterns": {
            "total": pattern_stats["total"],
            "average_score": round(pattern_stats["avg_score"] or 0, 3),
            "by_type": pattern_types
        },
        "error_resolutions": {
            "unique_errors": error_stats["total"],
            "total_occurrences": error_stats["total_occurrences"]
        },
        "tool_effectiveness": {
            "tracked_use_cases": tool_stats["total"],
            "total_successes": tool_stats["total_successes"],
            "total_failures": tool_stats["total_failures"]
        },
        "recent_patterns": recent,
        "db_path": str(DB_PATH)
    }


def export_data(format: str = "json") -> dict:
    """Export all data"""
    conn = init_db()
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM patterns ORDER BY created_at DESC")
    patterns = [dict(row) for row in cursor.fetchall()]

    cursor.execute("SELECT * FROM error_resolutions ORDER BY occurrence_count DESC")
    errors = [dict(row) for row in cursor.fetchall()]

    cursor.execute("SELECT * FROM tool_effectiveness ORDER BY last_used DESC")
    tools = [dict(row) for row in cursor.fetchall()]

    conn.close()

    return {
        "exported_at": datetime.now().isoformat(),
        "patterns": patterns,
        "error_resolutions": errors,
        "tool_effectiveness": tools
    }


# =============================================================================
# CLI Interface
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Reasoning Bank - Pattern tracking for learned strategies",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s record --type workflow --context "refactoring auth" --action "extracted shared utils" --score 0.9
  %(prog)s query --type code --context "refactoring"
  %(prog)s error --type ImportError --signature "No module named 'foo'" --resolution "pip install foo"
  %(prog)s tool --name grep --use-case "finding definitions" --success
  %(prog)s feedback --id 42 --score 1.0 --note "worked perfectly"
  %(prog)s stats
  %(prog)s export
        """
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # Record command
    record_parser = subparsers.add_parser("record", help="Record a new pattern")
    record_parser.add_argument("--type", "-t", required=True, choices=VALID_PATTERN_TYPES, help="Pattern type")
    record_parser.add_argument("--context", "-c", required=True, help="Situation/context that triggered the action")
    record_parser.add_argument("--action", "-a", required=True, help="What was done")
    record_parser.add_argument("--outcome", "-o", help="Result of the action")
    record_parser.add_argument("--score", "-s", type=float, default=0.5, help="Success score 0.0-1.0 (default: 0.5)")
    record_parser.add_argument("--feedback", "-f", help="Additional feedback or notes")
    record_parser.add_argument("--tags", nargs="+", help="Tags for categorization")

    # Query command
    query_parser = subparsers.add_parser("query", help="Query recorded patterns")
    query_parser.add_argument("--type", "-t", choices=VALID_PATTERN_TYPES, help="Filter by pattern type")
    query_parser.add_argument("--context", "-c", help="Search context keywords")
    query_parser.add_argument("--min-score", type=float, default=0.0, help="Minimum success score")
    query_parser.add_argument("--limit", "-n", type=int, default=10, help="Max results (default: 10)")

    # Error command
    error_parser = subparsers.add_parser("error", help="Record/query error resolutions")
    error_parser.add_argument("--type", "-t", required=True, help="Error type (e.g., ImportError, TypeError)")
    error_parser.add_argument("--signature", "-s", help="Error message or signature")
    error_parser.add_argument("--resolution", "-r", help="How to resolve (required for recording)")
    error_parser.add_argument("--prevention", "-p", help="How to prevent in future")

    # Tool command
    tool_parser = subparsers.add_parser("tool", help="Track tool effectiveness")
    tool_parser.add_argument("--name", "-n", help="Tool name")
    tool_parser.add_argument("--use-case", "-u", help="Use case description (required for recording)")
    tool_parser.add_argument("--success", action="store_true", help="Mark as successful")
    tool_parser.add_argument("--failure", action="store_true", help="Mark as failed")
    tool_parser.add_argument("--duration", "-d", type=float, help="Duration in milliseconds")
    tool_parser.add_argument("--notes", help="Additional notes")

    # Feedback command
    feedback_parser = subparsers.add_parser("feedback", help="Update pattern with feedback")
    feedback_parser.add_argument("--id", "-i", type=int, required=True, help="Pattern ID to update")
    feedback_parser.add_argument("--score", "-s", type=float, required=True, help="New success score 0.0-1.0")
    feedback_parser.add_argument("--note", "-n", help="Feedback note")

    # Stats command
    subparsers.add_parser("stats", help="Show database statistics")

    # Export command
    export_parser = subparsers.add_parser("export", help="Export all data")
    export_parser.add_argument("--format", "-f", choices=["json"], default="json", help="Export format")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    result = None

    if args.command == "record":
        result = record_pattern(
            pattern_type=args.type,
            context=args.context,
            action=args.action,
            outcome=args.outcome,
            score=args.score,
            feedback=args.feedback,
            tags=args.tags
        )

    elif args.command == "query":
        result = query_patterns(
            pattern_type=args.type,
            context=args.context,
            min_score=args.min_score,
            limit=args.limit
        )

    elif args.command == "error":
        if args.resolution:
            # Recording mode
            result = record_error_resolution(
                error_type=args.type,
                signature=args.signature or "",
                resolution=args.resolution,
                prevention=args.prevention
            )
        else:
            # Query mode
            result = query_error_resolution(
                error_type=args.type,
                signature=args.signature
            )

    elif args.command == "tool":
        if args.use_case and (args.success or args.failure):
            # Recording mode
            result = record_tool_usage(
                tool_name=args.name,
                use_case=args.use_case,
                success=args.success,
                duration_ms=args.duration,
                notes=args.notes
            )
        else:
            # Query mode
            result = query_tool_effectiveness(tool_name=args.name)

    elif args.command == "feedback":
        result = update_pattern_score(
            pattern_id=args.id,
            score=args.score,
            feedback=args.note
        )

    elif args.command == "stats":
        result = get_stats()

    elif args.command == "export":
        result = export_data(format=args.format)

    # Output JSON
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
