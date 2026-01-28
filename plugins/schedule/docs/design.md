---
title: Scheduler Agent Design
type: design-doc
status: draft
version: "0.1"
created: "2026-01-28"
author: "Asha"
---

# Scheduler Agent Design

## Overview

A Claude Code agent that manages scheduled task execution through external schedulers (cron/systemd). The agent writes task definitions; external infrastructure executes them.

**Core Principle**: Agent manages metadata, never executes scheduling directly.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        User Interaction                          │
│  /schedule "Every weekday at 9am" "Review code changes"         │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                      Scheduler Agent                             │
│  - Parses natural language → cron expression                    │
│  - Validates schedule and permissions                           │
│  - Writes to .claude/schedules.json                             │
│  - Triggers sync script                                         │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                    .claude/schedules.json                        │
│  Task definitions (version-controlled, auditable)               │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                      sync-schedules.py                           │
│  - Reads schedules.json                                         │
│  - Detects platform (systemd vs cron)                           │
│  - Generates appropriate scheduler entries                      │
│  - Updates crontab or systemd user timers                       │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│              External Scheduler (cron/systemd)                   │
│  At scheduled time:                                             │
│  → Invokes task-runner.sh                                       │
│  → task-runner.sh calls: claude -p "command" --allowedTools ... │
│  → Output logged to .claude/logs/{task-id}.log                  │
└─────────────────────────────────────────────────────────────────┘
```

---

## Components

### 1. Agent Definition

**File**: `plugins/asha/agents/scheduler.md`

```markdown
---
title: Scheduler
type: agent
domain: automation
version: "1.0"
tools:
  - Read
  - Write
  - Edit
  - Bash
  - Grep
  - Glob
---

# Scheduler Agent

## Purpose

Manage scheduled Claude Code task execution through external schedulers.

## Capabilities

- Parse natural language time expressions to cron syntax
- Create, update, delete scheduled task definitions
- Validate task permissions and security constraints
- Trigger scheduler synchronization
- Report task execution history

## Constraints

- Never modify crontab directly (use sync script)
- Enforce rate limits (max 10 tasks/project, 5 new/hour)
- Default to read-only permissions for new tasks
- Require explicit opt-in for write-capable tasks

## Task Definition Schema

Tasks are stored in `.claude/schedules.json`:

{
  "version": "1.0",
  "tasks": [
    {
      "id": "string (unique)",
      "enabled": "boolean",
      "name": "string (human-readable)",
      "schedule": "string (cron expression)",
      "scheduleHuman": "string (original natural language)",
      "command": "string (prompt to execute)",
      "workingDirectory": "string (absolute path)",
      "timeout": "number (seconds, default 300)",
      "permissions": {
        "allowedTools": ["array of tool names"],
        "readOnly": "boolean (default true)"
      },
      "notifications": {
        "onSuccess": "boolean",
        "onFailure": "boolean",
        "method": "string (log|email|webhook)"
      },
      "created": "ISO 8601 timestamp",
      "createdBy": "string (session ID)",
      "lastRun": "ISO 8601 timestamp or null",
      "lastStatus": "string (success|failure|timeout|skipped)",
      "nextRun": "ISO 8601 timestamp"
    }
  ]
}

## Natural Language Parsing

| Input | Cron Expression |
|-------|-----------------|
| "Every day at 9am" | `0 9 * * *` |
| "Every weekday at 9am" | `0 9 * * 1-5` |
| "Every Monday at 2pm" | `0 14 * * 1` |
| "Every hour" | `0 * * * *` |
| "Every 15 minutes" | `*/15 * * * *` |
| "Daily at midnight" | `0 0 * * *` |
| "First of every month" | `0 0 1 * *` |

## Security Model

### Permission Levels

| Level | allowedTools | Use Case |
|-------|--------------|----------|
| **read-only** (default) | Read, Grep, Glob | Reports, analysis |
| **limited-write** | + Edit, Write | Code fixes, updates |
| **autonomous** | + Bash, Task | Full automation |

### Rate Limits

- Max 10 scheduled tasks per project
- Max 5 new tasks created per hour
- Max 1 autonomous task per project

### Validation Rules

- Command length ≤ 1000 characters
- Working directory must exist and be owned by user
- Cron expression must be valid syntax
- No shell metacharacters in command (prevent injection)

## Workflow

### Creating a Task

1. User invokes `/schedule "time" "command"`
2. Agent parses natural language → cron expression
3. Agent validates:
   - Rate limits not exceeded
   - No duplicate task exists
   - Working directory valid
   - Command safe
4. Agent generates unique ID (adjective-noun-000)
5. Agent writes to .claude/schedules.json
6. Agent triggers sync: `~/.claude/scripts/sync-schedules.py`
7. Agent confirms with next run time

### Listing Tasks

1. User invokes `/schedule list`
2. Agent reads .claude/schedules.json
3. Agent displays table:
   - ID, Name, Schedule, Next Run, Status, Enabled

### Removing a Task

1. User invokes `/schedule remove <id>`
2. Agent removes from schedules.json
3. Agent triggers sync (removes from scheduler)
4. Agent confirms removal

### Viewing Logs

1. User invokes `/schedule logs <id>`
2. Agent reads .claude/logs/{id}.log
3. Agent displays recent execution output
```

---

### 2. Command Definition

**File**: `plugins/asha/commands/schedule.md`

```markdown
---
name: schedule
description: "Manage scheduled Claude Code tasks"
argument-hint: "<action> [args] | \"time\" \"command\""
allowed-tools:
  - Read
  - Write
  - Edit
  - Bash
  - Grep
---

# /schedule Command

## Usage

```
/schedule "time expression" "command to run"
/schedule list
/schedule show <id>
/schedule remove <id>
/schedule enable <id>
/schedule disable <id>
/schedule logs <id>
/schedule sync
```

## Examples

### Create a scheduled task
```
/schedule "Every weekday at 9am" "Review code changes since yesterday"
/schedule "Daily at 2am" "Run test suite and report failures"
/schedule "Every Monday at 10am" "Generate weekly status report"
```

### Manage tasks
```
/schedule list                    # Show all scheduled tasks
/schedule show daily-review-001   # Show task details
/schedule disable daily-review-001
/schedule enable daily-review-001
/schedule remove daily-review-001
```

### View execution history
```
/schedule logs daily-review-001   # Show recent execution output
```

### Force sync (after manual edits)
```
/schedule sync                    # Re-sync schedules.json to crontab
```

## Behavior

1. **Task Creation**: Parses natural language time, validates constraints, writes to `.claude/schedules.json`, triggers sync
2. **Task Listing**: Reads schedules.json, displays formatted table with next run times
3. **Task Removal**: Removes from JSON, triggers sync to remove from scheduler
4. **Sync**: Invokes `sync-schedules.py` to update crontab/systemd

## Output Format

### List Output
```
ID                  | Schedule      | Next Run           | Status  | Enabled
--------------------|---------------|--------------------|---------|---------
daily-review-001    | 0 9 * * 1-5   | 2026-01-29 09:00   | success | yes
nightly-tests-002   | 0 2 * * *     | 2026-01-29 02:00   | failure | yes
weekly-report-003   | 0 10 * * 1    | 2026-02-03 10:00   | pending | yes
```

### Creation Confirmation
```
Task created: daily-review-001
  Schedule: Every weekday at 9am (0 9 * * 1-5)
  Command: Review code changes since yesterday
  Next run: 2026-01-29 09:00 UTC
  Permissions: read-only (Read, Grep, Glob)
```

## Error Handling

| Error | Message |
|-------|---------|
| Rate limit exceeded | "Rate limit: Max 10 tasks per project (current: 10)" |
| Invalid time expression | "Could not parse time expression. Try: 'Every day at 9am'" |
| Duplicate task | "Similar task already exists: {id}" |
| Task not found | "Task not found: {id}" |
| Sync failed | "Sync failed: {error}. Run `/schedule sync` to retry." |
```

---

### 3. Sync Script

**File**: `plugins/asha/tools/sync-schedules.py`

```python
#!/usr/bin/env python3
"""
Synchronize .claude/schedules.json to system scheduler (cron or systemd).

Usage:
    sync-schedules.py [--project-dir PATH] [--dry-run]

Safety:
    - Only modifies CLAUDE-MANAGED section of crontab
    - Creates backup before modification
    - Validates all entries before applying
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

# Constants
CRONTAB_MARKER = "# CLAUDE-MANAGED-TASKS"
CRONTAB_END = "# END-CLAUDE-MANAGED-TASKS"
SYSTEMD_USER_DIR = Path.home() / ".config/systemd/user"
BACKUP_DIR = Path.home() / ".claude/backups"


def detect_scheduler() -> str:
    """Detect available scheduler: systemd or cron."""
    try:
        result = subprocess.run(
            ["systemctl", "--user", "status"],
            capture_output=True,
            timeout=5
        )
        if result.returncode in (0, 3):  # 0=running, 3=no units
            return "systemd"
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    try:
        subprocess.run(["crontab", "-l"], capture_output=True, timeout=5)
        return "cron"
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    return "none"


def validate_cron_expression(expr: str) -> bool:
    """Validate cron expression syntax."""
    parts = expr.split()
    if len(parts) != 5:
        return False

    patterns = [
        r'^(\*|[0-5]?\d)(/\d+)?$',           # minute
        r'^(\*|[01]?\d|2[0-3])(/\d+)?$',     # hour
        r'^(\*|[1-9]|[12]\d|3[01])(/\d+)?$', # day of month
        r'^(\*|[1-9]|1[0-2])(/\d+)?$',       # month
        r'^(\*|[0-6])(-[0-6])?(/\d+)?$',     # day of week
    ]

    for part, pattern in zip(parts, patterns):
        # Handle ranges and lists
        for segment in part.split(','):
            segment = segment.split('-')[0].split('/')[0]
            if segment != '*' and not segment.isdigit():
                return False

    return True


def load_schedules(project_dir: Path) -> dict:
    """Load schedules.json from project."""
    schedules_file = project_dir / ".claude/schedules.json"
    if not schedules_file.exists():
        return {"version": "1.0", "tasks": []}

    with open(schedules_file) as f:
        return json.load(f)


def backup_crontab() -> Optional[Path]:
    """Backup current crontab."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    backup_file = BACKUP_DIR / f"crontab-{datetime.now():%Y%m%d-%H%M%S}.bak"

    try:
        result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
        if result.returncode == 0:
            backup_file.write_text(result.stdout)
            return backup_file
    except Exception:
        pass

    return None


def sync_to_cron(tasks: list, project_dir: Path, dry_run: bool = False) -> dict:
    """Sync tasks to user crontab."""
    # Backup first
    if not dry_run:
        backup_crontab()

    # Read existing crontab
    result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    existing_lines = result.stdout.splitlines() if result.returncode == 0 else []

    # Remove existing CLAUDE-MANAGED section
    new_lines = []
    in_managed_section = False
    for line in existing_lines:
        if line.strip() == CRONTAB_MARKER:
            in_managed_section = True
            continue
        if line.strip() == CRONTAB_END:
            in_managed_section = False
            continue
        if not in_managed_section:
            new_lines.append(line)

    # Add new CLAUDE-MANAGED section
    if tasks:
        new_lines.append("")
        new_lines.append(CRONTAB_MARKER)
        new_lines.append(f"# Generated: {datetime.utcnow():%Y-%m-%d %H:%M UTC}")
        new_lines.append(f"# Project: {project_dir}")

        for task in tasks:
            if not task.get("enabled", True):
                continue

            tools = ",".join(task.get("permissions", {}).get("allowedTools", ["Read", "Grep", "Glob"]))
            timeout = task.get("timeout", 300)
            log_file = project_dir / f".claude/logs/{task['id']}.log"

            cmd = (
                f"cd {task['workingDirectory']} && "
                f"timeout {timeout} claude -p \"{task['command']}\" "
                f"--allowedTools \"{tools}\" "
                f">> {log_file} 2>&1"
            )

            new_lines.append(f"# Task: {task['id']} - {task.get('name', 'Unnamed')}")
            new_lines.append(f"{task['schedule']} {cmd}")

        new_lines.append(CRONTAB_END)

    new_crontab = "\n".join(new_lines) + "\n"

    if dry_run:
        return {
            "success": True,
            "method": "cron",
            "dry_run": True,
            "content": new_crontab,
            "task_count": len([t for t in tasks if t.get("enabled", True)])
        }

    # Apply new crontab
    proc = subprocess.run(
        ["crontab", "-"],
        input=new_crontab,
        text=True,
        capture_output=True
    )

    if proc.returncode != 0:
        return {
            "success": False,
            "method": "cron",
            "error": proc.stderr
        }

    return {
        "success": True,
        "method": "cron",
        "task_count": len([t for t in tasks if t.get("enabled", True)])
    }


def sync_to_systemd(tasks: list, project_dir: Path, dry_run: bool = False) -> dict:
    """Sync tasks to systemd user timers."""
    SYSTEMD_USER_DIR.mkdir(parents=True, exist_ok=True)

    # Remove existing claude-task-* units
    for f in SYSTEMD_USER_DIR.glob("claude-task-*.service"):
        if not dry_run:
            f.unlink()
    for f in SYSTEMD_USER_DIR.glob("claude-task-*.timer"):
        if not dry_run:
            subprocess.run(["systemctl", "--user", "disable", f.name], capture_output=True)
            f.unlink()

    created = []
    for task in tasks:
        if not task.get("enabled", True):
            continue

        task_id = task["id"]
        tools = ",".join(task.get("permissions", {}).get("allowedTools", ["Read", "Grep", "Glob"]))
        timeout = task.get("timeout", 300)
        log_file = project_dir / f".claude/logs/{task_id}.log"

        # Generate service file
        service_content = f"""[Unit]
Description=Claude Task: {task.get('name', task_id)}

[Service]
Type=oneshot
WorkingDirectory={task['workingDirectory']}
ExecStart=/bin/bash -c 'claude -p "{task['command']}" --allowedTools "{tools}" >> {log_file} 2>&1'
TimeoutStartSec={timeout}
"""

        # Convert cron to OnCalendar (simplified)
        on_calendar = cron_to_oncalendar(task["schedule"])

        timer_content = f"""[Unit]
Description=Timer for Claude Task: {task.get('name', task_id)}

[Timer]
OnCalendar={on_calendar}
Persistent=true

[Install]
WantedBy=timers.target
"""

        service_file = SYSTEMD_USER_DIR / f"claude-task-{task_id}.service"
        timer_file = SYSTEMD_USER_DIR / f"claude-task-{task_id}.timer"

        if not dry_run:
            service_file.write_text(service_content)
            timer_file.write_text(timer_content)

            subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
            subprocess.run(["systemctl", "--user", "enable", timer_file.name], capture_output=True)
            subprocess.run(["systemctl", "--user", "start", timer_file.name], capture_output=True)

        created.append(task_id)

    return {
        "success": True,
        "method": "systemd",
        "dry_run": dry_run,
        "task_count": len(created),
        "tasks": created
    }


def cron_to_oncalendar(cron_expr: str) -> str:
    """Convert cron expression to systemd OnCalendar format (simplified)."""
    parts = cron_expr.split()
    if len(parts) != 5:
        return "*-*-* *:*:00"  # fallback

    minute, hour, dom, month, dow = parts

    # Handle common patterns
    if cron_expr == "0 * * * *":
        return "hourly"
    if cron_expr == "0 0 * * *":
        return "daily"
    if cron_expr == "0 0 * * 0":
        return "weekly"
    if cron_expr == "0 0 1 * *":
        return "monthly"

    # Build OnCalendar string
    dow_map = {"0": "Sun", "1": "Mon", "2": "Tue", "3": "Wed", "4": "Thu", "5": "Fri", "6": "Sat"}

    result_dow = ""
    if dow != "*":
        if "-" in dow:
            start, end = dow.split("-")
            result_dow = f"{dow_map.get(start, start)}-{dow_map.get(end, end)} "
        else:
            result_dow = f"{dow_map.get(dow, dow)} "

    result_date = f"*-{month if month != '*' else '*'}-{dom if dom != '*' else '*'}"
    result_time = f"{hour if hour != '*' else '*'}:{minute if minute != '*' else '*'}:00"

    return f"{result_dow}{result_date} {result_time}"


def main():
    parser = argparse.ArgumentParser(description="Sync Claude schedules to system scheduler")
    parser.add_argument("--project-dir", type=Path, default=Path.cwd())
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done")
    args = parser.parse_args()

    # Load schedules
    schedules = load_schedules(args.project_dir)
    tasks = schedules.get("tasks", [])

    # Validate tasks
    valid_tasks = []
    for task in tasks:
        if not validate_cron_expression(task.get("schedule", "")):
            print(f"Warning: Invalid cron expression for task {task.get('id')}: {task.get('schedule')}", file=sys.stderr)
            continue
        valid_tasks.append(task)

    # Detect and sync to scheduler
    scheduler = detect_scheduler()

    if scheduler == "systemd":
        result = sync_to_systemd(valid_tasks, args.project_dir, args.dry_run)
    elif scheduler == "cron":
        result = sync_to_cron(valid_tasks, args.project_dir, args.dry_run)
    else:
        result = {"success": False, "error": "No scheduler available (cron or systemd)"}

    print(json.dumps(result, indent=2))
    sys.exit(0 if result.get("success") else 1)


if __name__ == "__main__":
    main()
```

---

### 4. Task Runner Script

**File**: `plugins/asha/tools/task-runner.sh`

```bash
#!/usr/bin/env bash
set -euo pipefail

# Task runner wrapper for scheduled Claude executions
# Handles logging, timeout, and status tracking

TASK_ID="${1:?Task ID required}"
PROJECT_DIR="${2:?Project directory required}"
COMMAND="${3:?Command required}"
ALLOWED_TOOLS="${4:-Read,Grep,Glob}"
TIMEOUT="${5:-300}"

LOG_DIR="$PROJECT_DIR/.claude/logs"
LOG_FILE="$LOG_DIR/$TASK_ID.log"
STATUS_FILE="$LOG_DIR/$TASK_ID.status"

mkdir -p "$LOG_DIR"

# Log header
{
    echo "========================================"
    echo "Task: $TASK_ID"
    echo "Started: $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
    echo "Command: $COMMAND"
    echo "Timeout: ${TIMEOUT}s"
    echo "========================================"
} >> "$LOG_FILE"

# Execute
START_TIME=$(date +%s)
cd "$PROJECT_DIR"

if timeout "$TIMEOUT" claude -p "$COMMAND" --allowedTools "$ALLOWED_TOOLS" >> "$LOG_FILE" 2>&1; then
    STATUS="success"
    EXIT_CODE=0
else
    EXIT_CODE=$?
    if [ $EXIT_CODE -eq 124 ]; then
        STATUS="timeout"
    else
        STATUS="failure"
    fi
fi

END_TIME=$(date +%s)
DURATION=$((END_TIME - START_TIME))

# Log footer
{
    echo "========================================"
    echo "Finished: $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
    echo "Duration: ${DURATION}s"
    echo "Status: $STATUS (exit code: $EXIT_CODE)"
    echo "========================================"
    echo ""
} >> "$LOG_FILE"

# Update status file
cat > "$STATUS_FILE" << EOF
{
    "taskId": "$TASK_ID",
    "lastRun": "$(date -u '+%Y-%m-%dT%H:%M:%SZ')",
    "status": "$STATUS",
    "exitCode": $EXIT_CODE,
    "duration": $DURATION
}
EOF

# Update schedules.json lastRun and lastStatus
if command -v jq &>/dev/null; then
    SCHEDULES_FILE="$PROJECT_DIR/.claude/schedules.json"
    if [ -f "$SCHEDULES_FILE" ]; then
        TEMP_FILE=$(mktemp)
        jq --arg id "$TASK_ID" \
           --arg status "$STATUS" \
           --arg lastRun "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" \
           '(.tasks[] | select(.id == $id)) |= . + {lastRun: $lastRun, lastStatus: $status}' \
           "$SCHEDULES_FILE" > "$TEMP_FILE" && mv "$TEMP_FILE" "$SCHEDULES_FILE"
    fi
fi

exit $EXIT_CODE
```

---

### 5. Hook Integration

**Addition to**: `plugins/asha/hooks/hooks.json`

```json
{
  "hooks": {
    "SessionStart": [{
      "matcher": "*",
      "hooks": [{
        "type": "command",
        "command": "${CLAUDE_PLUGIN_ROOT}/hooks/handlers/session-start.sh"
      }]
    }]
  }
}
```

**Addition to**: `plugins/asha/hooks/handlers/session-start.sh`

```bash
# Add to session-start.sh after existing initialization

# Inject scheduled tasks context
SCHEDULES_FILE="$PROJECT_DIR/.claude/schedules.json"
if [ -f "$SCHEDULES_FILE" ]; then
    PENDING_TASKS=$(jq -r '
        .tasks[] |
        select(.enabled == true) |
        "- \(.name // .id): \(.scheduleHuman // .schedule) (next: \(.nextRun // "unknown"))"
    ' "$SCHEDULES_FILE" 2>/dev/null || echo "")

    if [ -n "$PENDING_TASKS" ]; then
        ADDITIONAL_CONTEXT="$ADDITIONAL_CONTEXT

## Scheduled Tasks
$PENDING_TASKS"
    fi
fi
```

---

## Security Considerations

### Input Validation

```python
def validate_command(command: str) -> tuple[bool, str]:
    """Validate command safety."""
    if len(command) > 1000:
        return False, "Command too long (max 1000 characters)"

    # Block dangerous patterns
    dangerous = [
        r'rm\s+-rf',
        r'mkfs\.',
        r'dd\s+if=',
        r':\(\)\s*\{',  # fork bomb
        r'>\s*/dev/sd',
        r'chmod\s+-R\s+777',
    ]
    for pattern in dangerous:
        if re.search(pattern, command, re.IGNORECASE):
            return False, f"Dangerous pattern detected: {pattern}"

    return True, ""


def validate_rate_limits(project_dir: Path, schedules: dict) -> tuple[bool, str]:
    """Check rate limits."""
    tasks = schedules.get("tasks", [])

    if len(tasks) >= 10:
        return False, "Maximum 10 scheduled tasks per project"

    # Check creation rate
    one_hour_ago = datetime.utcnow() - timedelta(hours=1)
    recent_count = sum(
        1 for t in tasks
        if datetime.fromisoformat(t.get("created", "2000-01-01").replace("Z", "")) > one_hour_ago
    )
    if recent_count >= 5:
        return False, "Rate limit: Maximum 5 new tasks per hour"

    return True, ""
```

### Audit Logging

All task operations logged to `.claude/logs/scheduler-audit.log`:

```
2026-01-28T14:30:00Z CREATE task=daily-review-001 user=pknull session=abc123
2026-01-28T14:35:00Z DISABLE task=daily-review-001 user=pknull session=abc123
2026-01-28T14:40:00Z DELETE task=daily-review-001 user=pknull session=abc123
```

---

## Installation

### Prerequisites

- Claude Code CLI installed and authenticated
- Python 3.8+ (for sync script)
- `jq` (optional, for status updates)
- cron or systemd (for task execution)

### Setup

1. Install the asha plugin (includes scheduler)
2. Run `/schedule sync` to initialize
3. Create first task: `/schedule "Every day at 9am" "Check for updates"`

### Verification

```bash
# Check crontab entries
crontab -l | grep CLAUDE-MANAGED

# Or check systemd timers
systemctl --user list-timers | grep claude-task
```

---

## Future Enhancements

1. **Git worktree isolation**: Run autonomous tasks in separate branch
2. **Notification integrations**: Slack, email, webhook on task completion
3. **Task dependencies**: Run task B after task A completes
4. **Retry logic**: Automatic retry with exponential backoff
5. **Resource limits**: CPU/memory constraints per task
6. **Web dashboard**: View and manage tasks via browser

---

## Open Questions

1. **Cross-project tasks**: Should global tasks (not project-specific) be supported?
2. **Task templates**: Pre-defined common tasks (daily review, weekly report)?
3. **Approval workflow**: Require approval before autonomous tasks run?
4. **Cost tracking**: Estimate/track API usage per scheduled task?
