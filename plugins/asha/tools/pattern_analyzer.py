#!/usr/bin/env python3
"""
Pattern Analyzer - Extract learnings and synthesize Memory from session events

Reads events.jsonl, identifies recurring patterns, extracts calibration signals,
and synthesizes Memory files using the Four Questions structure.

Usage:
    python pattern_analyzer.py synthesize [--session-id ID]
    python pattern_analyzer.py patterns [--min-confidence 0.7]
    python pattern_analyzer.py calibration [--session-id ID]
"""

import os
import re
import json
import argparse
import subprocess
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List, Tuple
from collections import defaultdict, Counter


def detect_project_root() -> Path:
    """Find project root via environment, git, or upward search for Memory/"""
    claude_project_dir = os.environ.get("CLAUDE_PROJECT_DIR")
    if claude_project_dir:
        project_path = Path(claude_project_dir)
        if (project_path / "Memory").is_dir():
            return project_path

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

    search_dir = Path(__file__).parent.resolve()
    while search_dir != search_dir.parent:
        if (search_dir / "Memory").is_dir():
            return search_dir
        search_dir = search_dir.parent

    raise RuntimeError("Cannot detect project root. Ensure Memory/ directory exists.")


# Paths
PROJECT_ROOT = detect_project_root()
EVENTS_FILE = PROJECT_ROOT / "Memory" / "events" / "events.jsonl"
ACTIVE_CONTEXT = PROJECT_ROOT / "Memory" / "activeContext.md"
LEARNINGS_FILE = Path.home() / ".asha" / "learnings.md"
VOICE_FILE = Path.home() / ".asha" / "voice.md"
KEEPER_FILE = Path.home() / ".asha" / "keeper.md"
PATTERNS_FILE = PROJECT_ROOT / "Memory" / "events" / "patterns.json"


# Calibration signal patterns
VOICE_PATTERNS = [
    (r"too (formal|verbose|casual|terse|whimsical|wordy|brief)", "tone"),
    (r"(less|more) (formal|verbose|casual|detailed|concise)", "tone"),
    (r"don'?t be so (formal|verbose|casual|wordy)", "tone"),
    (r"(stop|quit|don'?t) (using|with) (emojis?|exclamation)", "style"),
]

KEEPER_PATTERNS = [
    (r"I (prefer|like|want|need|always)", "preference"),
    (r"(remember|note) that I", "preference"),
    (r"my (preference|style|workflow) is", "preference"),
    (r"I('m| am) (a|an) (\w+) (developer|engineer|writer)", "identity"),
]


def load_events(session_id: Optional[str] = None, days: int = 7) -> List[Dict]:
    """Load events from JSONL file with optional filtering"""
    if not EVENTS_FILE.exists():
        return []

    events = []
    cutoff = (datetime.now() - timedelta(days=days)).isoformat() + "Z"

    with open(EVENTS_FILE, 'r') as f:
        for line in f:
            if not line.strip():
                continue
            try:
                event = json.loads(line)

                # Filter by session if specified
                if session_id and event.get("session_id") != session_id:
                    continue

                # Filter by date
                if event.get("timestamp", "") < cutoff:
                    continue

                events.append(event)
            except json.JSONDecodeError:
                continue

    return sorted(events, key=lambda e: e.get("timestamp", ""))


def get_last_session_id() -> Optional[str]:
    """Get the most recent session ID from events"""
    if not EVENTS_FILE.exists():
        return None

    last_session = None
    with open(EVENTS_FILE, 'r') as f:
        for line in f:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
                last_session = event.get("session_id")
            except json.JSONDecodeError:
                continue

    return last_session


def load_existing_patterns() -> Dict:
    """Load existing pattern confidence data"""
    if PATTERNS_FILE.exists():
        try:
            return json.loads(PATTERNS_FILE.read_text())
        except json.JSONDecodeError:
            pass
    return {"patterns": {}, "last_updated": None}


def save_patterns(data: Dict):
    """Save pattern confidence data"""
    PATTERNS_FILE.parent.mkdir(parents=True, exist_ok=True)
    data["last_updated"] = datetime.now().isoformat()
    PATTERNS_FILE.write_text(json.dumps(data, indent=2))


# =============================================================================
# Pattern Extraction
# =============================================================================

def extract_tool_sequences(events: List[Dict]) -> List[Tuple[str, str, int]]:
    """Extract recurring tool/agent sequences"""
    sequences = []

    # Get agent deployments in order
    agents = [
        e["payload"].get("agent_type")
        for e in events
        if e.get("subtype") == "agent_deployed" and e["payload"].get("agent_type")
    ]

    # Find pairs
    pair_counts = Counter()
    for i in range(len(agents) - 1):
        pair = (agents[i], agents[i + 1])
        pair_counts[pair] += 1

    # Return pairs that occurred more than once
    for (a, b), count in pair_counts.items():
        if count >= 2:
            sequences.append((a, b, count))

    return sequences


def extract_file_patterns(events: List[Dict]) -> List[Dict]:
    """Extract patterns in file modifications"""
    file_events = [
        e for e in events
        if e.get("subtype") in ("file_modified", "file_created")
    ]

    # Group by directory
    dir_counts = Counter()
    ext_counts = Counter()

    for e in file_events:
        path = e["payload"].get("file_path", "")
        if "/" in path:
            dir_counts[path.rsplit("/", 1)[0]] += 1
        if "." in path:
            ext_counts[path.rsplit(".", 1)[1]] += 1

    patterns = []
    for dir_path, count in dir_counts.most_common(5):
        if count >= 3:
            patterns.append({
                "type": "directory_focus",
                "path": dir_path,
                "count": count
            })

    return patterns


def extract_error_patterns(events: List[Dict]) -> List[Dict]:
    """Extract recurring error patterns"""
    errors = [
        e for e in events
        if e.get("subtype") == "error"
    ]

    # Group by tool
    by_tool = defaultdict(list)
    for e in errors:
        tool = e.get("metadata", {}).get("tool_name", "unknown")
        by_tool[tool].append(e["payload"].get("error", ""))

    patterns = []
    for tool, error_list in by_tool.items():
        if len(error_list) >= 2:
            patterns.append({
                "type": "recurring_error",
                "tool": tool,
                "count": len(error_list),
                "sample": error_list[0][:100] if error_list else ""
            })

    return patterns


def extract_calibration_signals(events: List[Dict]) -> Dict[str, List[Dict]]:
    """Extract voice and keeper calibration signals from decision events"""
    signals = {"voice": [], "keeper": []}

    decisions = [
        e for e in events
        if e.get("type") == "context" and e.get("subtype") == "decision"
    ]

    for event in decisions:
        text = event["payload"].get("detail", "")
        timestamp = event.get("timestamp", "")

        # Check voice patterns
        for pattern, category in VOICE_PATTERNS:
            if re.search(pattern, text, re.IGNORECASE):
                signals["voice"].append({
                    "text": text,
                    "category": category,
                    "timestamp": timestamp
                })
                break

        # Check keeper patterns
        for pattern, category in KEEPER_PATTERNS:
            if re.search(pattern, text, re.IGNORECASE):
                signals["keeper"].append({
                    "text": text,
                    "category": category,
                    "timestamp": timestamp
                })
                break

    return signals


# =============================================================================
# Four Questions Synthesis
# =============================================================================

def synthesize_accomplishments(events: List[Dict]) -> List[str]:
    """What was accomplished?"""
    accomplishments = []

    # File changes
    files_modified = set()
    files_created = set()

    for e in events:
        if e.get("subtype") == "file_modified":
            files_modified.add(e["payload"].get("file_path", ""))
        elif e.get("subtype") == "file_created":
            files_created.add(e["payload"].get("file_path", ""))

    if files_created:
        accomplishments.append(f"Created {len(files_created)} file(s): {', '.join(sorted(files_created)[:5])}")
    if files_modified:
        accomplishments.append(f"Modified {len(files_modified)} file(s): {', '.join(sorted(files_modified)[:5])}")

    # Agent deployments
    agents = [
        e["payload"].get("agent_type")
        for e in events
        if e.get("subtype") == "agent_deployed"
    ]
    if agents:
        agent_counts = Counter(agents)
        top_agents = [f"{a} ({c}x)" for a, c in agent_counts.most_common(3)]
        accomplishments.append(f"Deployed agents: {', '.join(top_agents)}")

    # Commands/skills used
    commands = [
        e["payload"].get("command")
        for e in events
        if e.get("subtype") == "command"
    ]
    if commands:
        accomplishments.append(f"Commands used: {', '.join(set(commands))}")

    return accomplishments if accomplishments else ["No significant changes recorded"]


def synthesize_learnings(events: List[Dict], existing_patterns: Dict) -> List[str]:
    """What was learned? (with confidence tracking)"""
    learnings = []
    patterns = existing_patterns.get("patterns", {})

    # Tool sequences
    sequences = extract_tool_sequences(events)
    for a, b, count in sequences:
        pattern_key = f"sequence:{a}->{b}"

        # Update confidence
        if pattern_key not in patterns:
            patterns[pattern_key] = {"count": 0, "confidence": 0.5}

        patterns[pattern_key]["count"] += count
        # Confidence grows with usage (capped at 0.95)
        new_conf = min(0.95, 0.5 + (patterns[pattern_key]["count"] * 0.05))
        patterns[pattern_key]["confidence"] = new_conf

        if new_conf >= 0.7:
            learnings.append(f"[auto, conf:{new_conf:.2f}] {a} → {b} is effective sequence")

    # Error patterns (negative learnings)
    error_patterns = extract_error_patterns(events)
    for ep in error_patterns:
        pattern_key = f"error:{ep['tool']}"
        if pattern_key not in patterns:
            patterns[pattern_key] = {"count": 0, "confidence": 0.5}

        patterns[pattern_key]["count"] += ep["count"]
        learnings.append(f"[auto, warning] {ep['tool']} errors ({ep['count']}x): {ep['sample'][:50]}")

    # Save updated patterns
    existing_patterns["patterns"] = patterns
    save_patterns(existing_patterns)

    return learnings if learnings else ["No new patterns detected"]


def synthesize_blockers(events: List[Dict]) -> List[str]:
    """What blockers exist?"""
    blockers = []

    # Recent errors
    errors = [e for e in events if e.get("subtype") == "error"]
    if errors:
        recent_errors = errors[-3:]  # Last 3 errors
        for e in recent_errors:
            error_text = e["payload"].get("error", "Unknown error")[:100]
            blockers.append(f"Error: {error_text}")

    # Blocked tasks
    blocked = [
        e for e in events
        if e.get("type") == "task" and e.get("subtype") == "blocked"
    ]
    for t in blocked:
        blockers.append(f"Blocked: {t['payload'].get('subject', 'Unknown task')}")

    return blockers if blockers else ["None detected"]


def synthesize_next_steps(events: List[Dict]) -> List[str]:
    """What's next?"""
    next_steps = []

    # Pending tasks from events
    tasks = {}
    for e in events:
        if e.get("type") == "task":
            task_id = e["payload"].get("id", e.get("id"))
            tasks[task_id] = e

    for task in tasks.values():
        status = task["payload"].get("status")
        if status in ("pending", "in_progress"):
            next_steps.append(f"[ ] {task['payload'].get('subject', 'Unknown task')}")

    # Infer from recent activity
    file_patterns = extract_file_patterns(events)
    for fp in file_patterns[:2]:
        if fp["type"] == "directory_focus":
            next_steps.append(f"[ ] Continue work in {fp['path']}/")

    return next_steps if next_steps else ["Review and plan next session"]


def generate_active_context(events: List[Dict], existing_patterns: Dict) -> str:
    """Generate activeContext.md content using Four Questions"""
    lines = []

    # Frontmatter
    lines.append("---")
    lines.append(f'version: "2.0"')
    lines.append(f'lastUpdated: "{datetime.now().strftime("%Y-%m-%d %H:%M")} UTC"')
    lines.append(f'lifecycle: "active"')
    lines.append(f'synthesizedFrom: "events"')
    lines.append("---")
    lines.append("")
    lines.append("# Active Context")
    lines.append("")

    # What was accomplished?
    lines.append("## What Was Accomplished")
    lines.append("")
    for item in synthesize_accomplishments(events):
        lines.append(f"- {item}")
    lines.append("")

    # What was learned?
    lines.append("## What Was Learned")
    lines.append("")
    for item in synthesize_learnings(events, existing_patterns):
        lines.append(f"- {item}")
    lines.append("")

    # What blockers exist?
    lines.append("## Current Blockers")
    lines.append("")
    for item in synthesize_blockers(events):
        lines.append(f"- {item}")
    lines.append("")

    # What's next?
    lines.append("## Next Steps")
    lines.append("")
    for item in synthesize_next_steps(events):
        lines.append(f"- {item}")
    lines.append("")

    return "\n".join(lines)


# =============================================================================
# Calibration Updates
# =============================================================================

def append_to_learnings(learnings: List[str], session_id: str):
    """Append new learnings to ~/.asha/learnings.md"""
    if not learnings or learnings == ["No new patterns detected"]:
        return

    LEARNINGS_FILE.parent.mkdir(parents=True, exist_ok=True)

    # Read existing content
    existing = ""
    if LEARNINGS_FILE.exists():
        existing = LEARNINGS_FILE.read_text()

    # Find or create "## Auto-Extracted" section
    section_header = "## Auto-Extracted Patterns"
    if section_header not in existing:
        existing += f"\n\n{section_header}\n\n"

    # Append new learnings with timestamp
    timestamp = datetime.now().strftime("%Y-%m-%d")
    new_entries = f"\n### {timestamp} (session: {session_id[:20]})\n\n"
    for learning in learnings:
        if learning != "No new patterns detected":
            new_entries += f"- {learning}\n"

    # Insert before next section or at end
    parts = existing.split(section_header)
    if len(parts) == 2:
        updated = parts[0] + section_header + parts[1].rstrip() + new_entries
    else:
        updated = existing + new_entries

    LEARNINGS_FILE.write_text(updated)


def append_to_voice(signals: List[Dict]):
    """Append voice calibration signals to ~/.asha/voice.md"""
    if not signals:
        return

    VOICE_FILE.parent.mkdir(parents=True, exist_ok=True)

    existing = ""
    if VOICE_FILE.exists():
        existing = VOICE_FILE.read_text()

    # Find or create calibration log section
    section_header = "## Calibration Log"
    if section_header not in existing:
        existing += f"\n\n{section_header}\n\n"

    # Append signals
    new_entries = ""
    for signal in signals:
        ts = signal["timestamp"][:10] if signal.get("timestamp") else datetime.now().strftime("%Y-%m-%d")
        new_entries += f"- {ts}: \"{signal['text'][:80]}\" ({signal['category']})\n"

    parts = existing.split(section_header)
    if len(parts) == 2:
        updated = parts[0] + section_header + parts[1].rstrip() + "\n" + new_entries
    else:
        updated = existing + new_entries

    VOICE_FILE.write_text(updated)


def append_to_keeper(signals: List[Dict]):
    """Append keeper calibration signals to ~/.asha/keeper.md"""
    if not signals:
        return

    KEEPER_FILE.parent.mkdir(parents=True, exist_ok=True)

    existing = ""
    if KEEPER_FILE.exists():
        existing = KEEPER_FILE.read_text()

    # Find calibration log section
    section_header = "## Calibration Log"
    if section_header not in existing:
        # Keeper file should already exist with structure
        return

    # Append to calibration log (inside the code block)
    timestamp = datetime.now().strftime("%Y-%m-%dT%H:%M:%S+10:00")
    project = PROJECT_ROOT.name

    new_entries = ""
    for signal in signals:
        text = signal["text"][:60].replace('"', "'")
        new_entries += f"{timestamp} | {project} | \"{text}\"\n"

    # Insert before closing ```
    if "```" in existing:
        parts = existing.rsplit("```", 1)
        updated = parts[0].rstrip() + "\n" + new_entries + "```" + parts[1]
        KEEPER_FILE.write_text(updated)


# =============================================================================
# Main Synthesis
# =============================================================================

def run_synthesis(session_id: Optional[str] = None, days: int = 7, skip_eval: bool = False) -> Dict:
    """Run full synthesis pipeline"""
    results = {
        "status": "success",
        "session_id": session_id,
        "events_processed": 0,
        "patterns_found": 0,
        "calibration_signals": {"voice": 0, "keeper": 0},
        "eval": None
    }

    # Load events
    events = load_events(session_id=session_id, days=days)
    results["events_processed"] = len(events)

    if not events:
        results["status"] = "no_events"
        return results

    # Get session ID from events if not specified
    if not session_id and events:
        session_id = events[-1].get("session_id", "unknown")
        results["session_id"] = session_id

    # Load existing patterns for confidence tracking
    existing_patterns = load_existing_patterns()

    # Generate activeContext.md
    active_context = generate_active_context(events, existing_patterns)
    ACTIVE_CONTEXT.write_text(active_context)

    # Extract and save learnings
    learnings = synthesize_learnings(events, existing_patterns)
    append_to_learnings(learnings, session_id or "unknown")
    results["patterns_found"] = len([l for l in learnings if l != "No new patterns detected"])

    # Extract and save calibration signals
    calibration = extract_calibration_signals(events)

    if calibration["voice"]:
        append_to_voice(calibration["voice"])
        results["calibration_signals"]["voice"] = len(calibration["voice"])

    if calibration["keeper"]:
        append_to_keeper(calibration["keeper"])
        results["calibration_signals"]["keeper"] = len(calibration["keeper"])

    # Session evaluation
    if not skip_eval:
        eval_result = evaluate_session(events, session_id or "unknown")
        save_eval_result(eval_result)
        results["eval"] = {
            "task_type": eval_result["task_type"],
            "score": eval_result["score"],
            "passed": eval_result["passed"]
        }

    return results


def check_orphaned_session(current_session_id: str) -> Optional[str]:
    """Check if there's an orphaned session that needs synthesis"""
    last_session = get_last_session_id()

    if last_session and last_session != current_session_id:
        # There's a previous session that might not have been synthesized
        # Check if it has events
        events = load_events(session_id=last_session, days=30)
        if events:
            return last_session

    return None


# =============================================================================
# Session Evaluation
# =============================================================================

EVAL_RESULTS_FILE = PROJECT_ROOT / "Memory" / "events" / "eval_history.jsonl"

# Task type detection patterns
TASK_TYPE_PATTERNS = {
    "feature": [
        r"\b(add|create|implement|build|new)\b.*\b(feature|functionality|endpoint|api|component)\b",
        r"\b(add|create|implement)\b",
    ],
    "bugfix": [
        r"\b(fix|bug|issue|error|broken|crash|fail)\b",
        r"\b(debug|resolve|repair)\b",
    ],
    "refactor": [
        r"\b(refactor|clean|reorganize|restructure|improve|optimize)\b",
        r"\b(move|extract|consolidate|simplify)\b",
    ],
    "docs": [
        r"\b(document|readme|docs|comment|explain)\b",
    ],
    "test": [
        r"\b(test|spec|coverage|tdd)\b",
    ],
}

# Eval criteria templates by task type
EVAL_TEMPLATES = {
    "feature": [
        {"name": "files_created", "weight": 0.3},
        {"name": "no_errors", "weight": 0.3},
        {"name": "tests_run", "weight": 0.2},
        {"name": "session_completed", "weight": 0.2},
    ],
    "bugfix": [
        {"name": "no_errors", "weight": 0.4},
        {"name": "tests_run", "weight": 0.3},
        {"name": "session_completed", "weight": 0.3},
    ],
    "refactor": [
        {"name": "files_modified", "weight": 0.3},
        {"name": "no_errors", "weight": 0.3},
        {"name": "tests_run", "weight": 0.2},
        {"name": "session_completed", "weight": 0.2},
    ],
    "docs": [
        {"name": "files_created_or_modified", "weight": 0.5},
        {"name": "no_errors", "weight": 0.2},
        {"name": "session_completed", "weight": 0.3},
    ],
    "test": [
        {"name": "files_created", "weight": 0.3},
        {"name": "tests_run", "weight": 0.4},
        {"name": "session_completed", "weight": 0.3},
    ],
    "unknown": [
        {"name": "no_errors", "weight": 0.4},
        {"name": "session_completed", "weight": 0.6},
    ],
}


def classify_task_type(events: List[Dict]) -> str:
    """Classify task type from session events (especially first user decision)"""
    # Look for first decision event (user's original request)
    decisions = [e for e in events if e.get("subtype") == "decision"]

    if not decisions:
        return "unknown"

    # Check first decision (original request)
    first_request = decisions[0].get("payload", {}).get("detail", "").lower()

    # Match against patterns
    for task_type, patterns in TASK_TYPE_PATTERNS.items():
        for pattern in patterns:
            if re.search(pattern, first_request, re.IGNORECASE):
                return task_type

    return "unknown"


def check_criterion(criterion_name: str, events: List[Dict]) -> Tuple[bool, str]:
    """Check a single criterion against events, return (passed, reason)"""

    if criterion_name == "files_created":
        created = [e for e in events if e.get("subtype") == "file_created"]
        if created:
            return True, f"{len(created)} files created"
        return False, "No files created"

    elif criterion_name == "files_modified":
        modified = [e for e in events if e.get("subtype") == "file_modified"]
        if modified:
            return True, f"{len(modified)} files modified"
        return False, "No files modified"

    elif criterion_name == "files_created_or_modified":
        created = [e for e in events if e.get("subtype") == "file_created"]
        modified = [e for e in events if e.get("subtype") == "file_modified"]
        total = len(created) + len(modified)
        if total > 0:
            return True, f"{total} files created/modified"
        return False, "No files created or modified"

    elif criterion_name == "no_errors":
        errors = [e for e in events if e.get("subtype") == "error"]
        if not errors:
            return True, "No errors"
        return False, f"{len(errors)} errors occurred"

    elif criterion_name == "tests_run":
        # Look for test-related agents or commands
        agents = [e for e in events if e.get("subtype") == "agent_deployed"]
        test_agents = [a for a in agents if "test" in a.get("payload", {}).get("agent_type", "").lower()
                      or "tdd" in a.get("payload", {}).get("agent_type", "").lower()]
        if test_agents:
            return True, f"Test agents deployed: {len(test_agents)}"

        # Check for test commands in skills
        commands = [e for e in events if e.get("subtype") == "command"]
        test_commands = [c for c in commands if "test" in c.get("payload", {}).get("command", "").lower()]
        if test_commands:
            return True, "Tests run via command"

        return False, "No tests detected"

    elif criterion_name == "session_completed":
        # Session is considered complete if there's substantial activity and no abandonment
        file_events = [e for e in events if e.get("subtype") in ("file_created", "file_modified")]
        errors = [e for e in events if e.get("subtype") == "error"]

        # Ratio of successful operations to errors
        if len(file_events) > 0:
            error_ratio = len(errors) / (len(file_events) + len(errors))
            if error_ratio < 0.5:  # Less than 50% error rate
                return True, f"Session productive ({len(file_events)} file ops, {len(errors)} errors)"

        # Check for any meaningful activity
        if len(events) >= 5:
            return True, f"Session had activity ({len(events)} events)"

        return False, "Session had minimal activity"

    elif criterion_name == "code_reviewed":
        agents = [e for e in events if e.get("subtype") == "agent_deployed"]
        review_agents = [a for a in agents if "review" in a.get("payload", {}).get("agent_type", "").lower()]
        if review_agents:
            return True, "Code review performed"
        return False, "No code review"

    else:
        return False, f"Unknown criterion: {criterion_name}"


def evaluate_session(events: List[Dict], session_id: str) -> Dict:
    """Evaluate session success based on task type and criteria"""

    if not events:
        return {
            "session_id": session_id,
            "task_type": "unknown",
            "score": 0.0,
            "passed": False,
            "criteria": [],
            "reason": "No events to evaluate"
        }

    # Classify task type
    task_type = classify_task_type(events)

    # Get criteria template
    criteria_template = EVAL_TEMPLATES.get(task_type, EVAL_TEMPLATES["unknown"])

    # Evaluate each criterion
    criteria_results = []
    weighted_score = 0.0

    for criterion in criteria_template:
        name = criterion["name"]
        weight = criterion["weight"]
        passed, reason = check_criterion(name, events)

        criteria_results.append({
            "name": name,
            "passed": passed,
            "weight": weight,
            "reason": reason
        })

        if passed:
            weighted_score += weight

    # Determine overall pass (threshold: 0.6)
    threshold = 0.6
    overall_passed = weighted_score >= threshold

    result = {
        "session_id": session_id,
        "timestamp": datetime.now().isoformat(),
        "task_type": task_type,
        "score": round(weighted_score, 2),
        "passed": overall_passed,
        "threshold": threshold,
        "criteria": criteria_results
    }

    return result


def save_eval_result(result: Dict):
    """Append eval result to history file"""
    EVAL_RESULTS_FILE.parent.mkdir(parents=True, exist_ok=True)

    with open(EVAL_RESULTS_FILE, 'a') as f:
        f.write(json.dumps(result) + '\n')


def get_eval_history(limit: int = 20) -> List[Dict]:
    """Get recent eval results"""
    if not EVAL_RESULTS_FILE.exists():
        return []

    results = []
    with open(EVAL_RESULTS_FILE, 'r') as f:
        for line in f:
            if line.strip():
                try:
                    results.append(json.loads(line))
                except json.JSONDecodeError:
                    pass

    # Return most recent
    return results[-limit:]


def get_eval_stats() -> Dict:
    """Get aggregate eval statistics"""
    history = get_eval_history(limit=100)

    if not history:
        return {"total": 0, "passed": 0, "failed": 0, "pass_rate": 0.0}

    passed = sum(1 for r in history if r.get("passed"))
    total = len(history)

    by_type = defaultdict(lambda: {"total": 0, "passed": 0})
    for r in history:
        task_type = r.get("task_type", "unknown")
        by_type[task_type]["total"] += 1
        if r.get("passed"):
            by_type[task_type]["passed"] += 1

    return {
        "total": total,
        "passed": passed,
        "failed": total - passed,
        "pass_rate": round(passed / total, 2) if total > 0 else 0.0,
        "avg_score": round(sum(r.get("score", 0) for r in history) / total, 2) if total > 0 else 0.0,
        "by_task_type": dict(by_type)
    }


# =============================================================================
# CLI
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Pattern Analyzer - Extract learnings and synthesize Memory",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # Synthesize command
    synth_parser = subparsers.add_parser("synthesize", help="Run full synthesis pipeline")
    synth_parser.add_argument("--session-id", "-s", help="Specific session to synthesize")
    synth_parser.add_argument("--days", "-d", type=int, default=7, help="Days of history (default: 7)")

    # Patterns command
    patterns_parser = subparsers.add_parser("patterns", help="Show extracted patterns")
    patterns_parser.add_argument("--min-confidence", "-c", type=float, default=0.0, help="Minimum confidence")

    # Calibration command
    cal_parser = subparsers.add_parser("calibration", help="Extract calibration signals only")
    cal_parser.add_argument("--session-id", "-s", help="Specific session to analyze")
    cal_parser.add_argument("--days", "-d", type=int, default=1, help="Days of history (default: 1)")

    # Check-orphan command
    orphan_parser = subparsers.add_parser("check-orphan", help="Check for orphaned session")
    orphan_parser.add_argument("--current-session", "-c", required=True, help="Current session ID")

    # Recover command
    recover_parser = subparsers.add_parser("recover", help="Recover and synthesize orphaned session")
    recover_parser.add_argument("--session-id", "-s", required=True, help="Orphaned session ID")

    # Eval command
    eval_parser = subparsers.add_parser("eval", help="Evaluate a session")
    eval_parser.add_argument("--session-id", "-s", help="Session to evaluate (default: current)")
    eval_parser.add_argument("--days", "-d", type=int, default=1, help="Days of history (default: 1)")

    # Eval history command
    history_parser = subparsers.add_parser("eval-history", help="Show eval history")
    history_parser.add_argument("--limit", "-n", type=int, default=10, help="Number of results")

    # Eval stats command
    subparsers.add_parser("eval-stats", help="Show aggregate eval statistics")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    try:
        if args.command == "synthesize":
            result = run_synthesis(session_id=args.session_id, days=args.days)
            print(json.dumps(result, indent=2))

        elif args.command == "patterns":
            data = load_existing_patterns()
            patterns = data.get("patterns", {})
            filtered = {
                k: v for k, v in patterns.items()
                if v.get("confidence", 0) >= args.min_confidence
            }
            print(json.dumps(filtered, indent=2))

        elif args.command == "calibration":
            events = load_events(session_id=args.session_id, days=args.days)
            signals = extract_calibration_signals(events)
            print(json.dumps(signals, indent=2))

        elif args.command == "check-orphan":
            orphan = check_orphaned_session(args.current_session)
            if orphan:
                print(json.dumps({"orphaned_session": orphan}))
            else:
                print(json.dumps({"orphaned_session": None}))

        elif args.command == "recover":
            result = run_synthesis(session_id=args.session_id, days=30)
            result["recovered"] = True
            print(json.dumps(result, indent=2))

        elif args.command == "eval":
            events = load_events(session_id=args.session_id, days=args.days)
            if not events:
                print(json.dumps({"error": "No events found"}))
            else:
                session_id = args.session_id or events[-1].get("session_id", "unknown")
                result = evaluate_session(events, session_id)
                print(json.dumps(result, indent=2))

        elif args.command == "eval-history":
            history = get_eval_history(limit=args.limit)
            print(json.dumps(history, indent=2))

        elif args.command == "eval-stats":
            stats = get_eval_stats()
            print(json.dumps(stats, indent=2))

    except Exception as e:
        import sys
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
