#!/usr/bin/env python3
"""
Scheduler management tool for Claude Code scheduled tasks.

Provides task creation, listing, removal, and management with
natural language time parsing and security validation.

Usage:
    scheduler.py create "Every weekday at 9am" "Review code changes"
    scheduler.py list
    scheduler.py show <task-id>
    scheduler.py remove <task-id>
    scheduler.py enable <task-id>
    scheduler.py disable <task-id>
    scheduler.py logs <task-id>
    scheduler.py sync
"""

import argparse
import json
import os
import random
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Import sibling module
sys.path.insert(0, str(Path(__file__).parent))
from time_parser import parse_time_expression, validate_cron


# Constants
SCHEDULES_FILE = ".claude/schedules.json"
LOGS_DIR = ".claude/logs"
AUDIT_LOG = ".claude/logs/scheduler-audit.log"

# Rate limits
MAX_TASKS_PER_PROJECT = 10
MAX_TASKS_PER_HOUR = 5
MAX_AUTONOMOUS_TASKS = 1

# ID generation words
ADJECTIVES = ['daily', 'nightly', 'weekly', 'monthly', 'hourly', 'quick', 'auto', 'smart', 'fresh', 'swift']
NOUNS = ['review', 'test', 'report', 'check', 'scan', 'build', 'sync', 'backup', 'clean', 'watch']

# Dangerous command patterns
DANGEROUS_PATTERNS = [
    r'rm\s+-rf',
    r'rm\s+-r\s+/',
    r'mkfs\.',
    r'dd\s+if=',
    r':\(\)\s*\{',  # fork bomb
    r'>\s*/dev/sd',
    r'chmod\s+-R\s+777',
    r'curl\s+.*\|\s*(?:ba)?sh',
    r'wget\s+.*\|\s*(?:ba)?sh',
]


def get_project_dir() -> Path:
    """Get project directory from environment or current directory."""
    if 'CLAUDE_PROJECT_DIR' in os.environ:
        return Path(os.environ['CLAUDE_PROJECT_DIR'])
    return Path.cwd()


def load_schedules(project_dir: Path) -> dict:
    """Load schedules.json from project."""
    schedules_file = project_dir / SCHEDULES_FILE
    if not schedules_file.exists():
        return {"version": "1.0", "tasks": []}

    with open(schedules_file) as f:
        return json.load(f)


def save_schedules(project_dir: Path, schedules: dict) -> None:
    """Save schedules.json to project."""
    schedules_file = project_dir / SCHEDULES_FILE
    schedules_file.parent.mkdir(parents=True, exist_ok=True)

    with open(schedules_file, 'w') as f:
        json.dump(schedules, f, indent=2)


def audit_log(project_dir: Path, action: str, task_id: str, details: str = "") -> None:
    """Append to audit log."""
    log_file = project_dir / AUDIT_LOG
    log_file.parent.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    session = os.environ.get('CLAUDE_SESSION_ID', 'unknown')
    user = os.environ.get('USER', 'unknown')

    entry = f"{timestamp} {action} task={task_id} user={user} session={session}"
    if details:
        entry += f" {details}"

    with open(log_file, 'a') as f:
        f.write(entry + "\n")


def generate_task_id(schedules: dict) -> str:
    """Generate unique task ID."""
    existing_ids = {t['id'] for t in schedules.get('tasks', [])}

    for _ in range(100):  # Try up to 100 times
        adj = random.choice(ADJECTIVES)
        noun = random.choice(NOUNS)
        num = random.randint(1, 999)
        task_id = f"{adj}-{noun}-{num:03d}"

        if task_id not in existing_ids:
            return task_id

    # Fallback to timestamp-based ID
    return f"task-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"


def validate_command(command: str) -> tuple[bool, str]:
    """Validate command for safety."""
    if not command or not command.strip():
        return False, "Command cannot be empty"

    if len(command) > 1000:
        return False, "Command too long (max 1000 characters)"

    # Check for dangerous patterns
    for pattern in DANGEROUS_PATTERNS:
        if re.search(pattern, command, re.IGNORECASE):
            return False, f"Command contains dangerous pattern"

    return True, ""


def check_rate_limits(schedules: dict) -> tuple[bool, str]:
    """Check rate limits for task creation."""
    tasks = schedules.get('tasks', [])

    # Max tasks per project
    if len(tasks) >= MAX_TASKS_PER_PROJECT:
        return False, f"Maximum {MAX_TASKS_PER_PROJECT} tasks per project (current: {len(tasks)})"

    # Max tasks created per hour
    one_hour_ago = datetime.now(timezone.utc).timestamp() - 3600
    recent_count = 0
    for task in tasks:
        created = task.get('created', '')
        if created:
            try:
                created_ts = datetime.fromisoformat(created.replace('Z', '+00:00')).timestamp()
                if created_ts > one_hour_ago:
                    recent_count += 1
            except ValueError:
                pass

    if recent_count >= MAX_TASKS_PER_HOUR:
        return False, f"Rate limit: Maximum {MAX_TASKS_PER_HOUR} new tasks per hour"

    return True, ""


def check_autonomous_limit(schedules: dict, allowed_tools: list) -> tuple[bool, str]:
    """Check autonomous task limit."""
    if 'Bash' not in allowed_tools:
        return True, ""

    # Count existing autonomous tasks
    autonomous_count = 0
    for task in schedules.get('tasks', []):
        task_tools = task.get('permissions', {}).get('allowedTools', [])
        if 'Bash' in task_tools and task.get('enabled', True):
            autonomous_count += 1

    if autonomous_count >= MAX_AUTONOMOUS_TASKS:
        return False, f"Maximum {MAX_AUTONOMOUS_TASKS} autonomous (Bash-enabled) task per project"

    return True, ""


def find_duplicate(schedules: dict, schedule: str, command: str) -> Optional[str]:
    """Find duplicate task with same schedule and command."""
    for task in schedules.get('tasks', []):
        if task.get('schedule') == schedule and task.get('command') == command:
            return task['id']
    return None


def create_task(
    project_dir: Path,
    time_expr: str,
    command: str,
    allowed_tools: Optional[list] = None,
    name: Optional[str] = None
) -> dict:
    """Create a new scheduled task."""
    # Parse time expression
    time_result = parse_time_expression(time_expr)
    if not time_result['success']:
        return {"success": False, "error": time_result['error']}

    cron = time_result['cron']
    human = time_result['human']

    # Validate command
    valid, error = validate_command(command)
    if not valid:
        return {"success": False, "error": error}

    # Load existing schedules
    schedules = load_schedules(project_dir)

    # Check rate limits
    valid, error = check_rate_limits(schedules)
    if not valid:
        return {"success": False, "error": error}

    # Set default allowed tools
    if allowed_tools is None:
        allowed_tools = ['Read', 'Grep', 'Glob']

    # Check autonomous limit
    valid, error = check_autonomous_limit(schedules, allowed_tools)
    if not valid:
        return {"success": False, "error": error}

    # Check for duplicates
    duplicate = find_duplicate(schedules, cron, command)
    if duplicate:
        return {"success": False, "error": f"Similar task already exists: {duplicate}"}

    # Generate task ID
    task_id = generate_task_id(schedules)

    # Create task
    task = {
        "id": task_id,
        "enabled": True,
        "name": name or command[:50],
        "schedule": cron,
        "scheduleHuman": human,
        "command": command,
        "workingDirectory": str(project_dir),
        "timeout": 300,
        "permissions": {
            "allowedTools": allowed_tools,
            "readOnly": 'Bash' not in allowed_tools and 'Edit' not in allowed_tools and 'Write' not in allowed_tools
        },
        "created": datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        "createdBy": os.environ.get('CLAUDE_SESSION_ID', 'unknown'),
        "lastRun": None,
        "lastStatus": None
    }

    # Add to schedules
    schedules['tasks'].append(task)
    save_schedules(project_dir, schedules)

    # Audit log
    audit_log(project_dir, 'CREATE', task_id, f"schedule={cron}")

    return {
        "success": True,
        "task": task,
        "message": f"Task created: {task_id}\n  Schedule: {human} ({cron})\n  Command: {command}\n  Permissions: {'read-only' if task['permissions']['readOnly'] else 'write-enabled'}"
    }


def list_tasks(project_dir: Path) -> dict:
    """List all scheduled tasks."""
    schedules = load_schedules(project_dir)
    tasks = schedules.get('tasks', [])

    if not tasks:
        return {"success": True, "tasks": [], "message": "No scheduled tasks"}

    # Format as table
    lines = [
        "ID                  | Schedule           | Next Run           | Status  | Enabled",
        "--------------------|--------------------|--------------------|---------|--------"
    ]

    for task in tasks:
        task_id = task.get('id', 'unknown')[:18].ljust(18)
        schedule = task.get('scheduleHuman', task.get('schedule', ''))[:18].ljust(18)
        last_status = (task.get('lastStatus') or 'pending')[:7].ljust(7)
        enabled = 'yes' if task.get('enabled', True) else 'no'
        enabled = enabled.ljust(6)

        # Calculate next run (simplified - just show schedule)
        next_run = task.get('nextRun', '-')[:18].ljust(18)

        lines.append(f"{task_id} | {schedule} | {next_run} | {last_status} | {enabled}")

    return {
        "success": True,
        "tasks": tasks,
        "message": "\n".join(lines)
    }


def show_task(project_dir: Path, task_id: str) -> dict:
    """Show details of a specific task."""
    schedules = load_schedules(project_dir)

    for task in schedules.get('tasks', []):
        if task.get('id') == task_id:
            return {"success": True, "task": task}

    return {"success": False, "error": f"Task not found: {task_id}"}


def update_task(project_dir: Path, task_id: str, enabled: Optional[bool] = None) -> dict:
    """Update a task (enable/disable)."""
    schedules = load_schedules(project_dir)

    for task in schedules.get('tasks', []):
        if task.get('id') == task_id:
            if enabled is not None:
                task['enabled'] = enabled
                save_schedules(project_dir, schedules)
                action = 'ENABLE' if enabled else 'DISABLE'
                audit_log(project_dir, action, task_id)
                return {
                    "success": True,
                    "task": task,
                    "message": f"Task {task_id} {'enabled' if enabled else 'disabled'}"
                }

    return {"success": False, "error": f"Task not found: {task_id}"}


def remove_task(project_dir: Path, task_id: str) -> dict:
    """Remove a task."""
    schedules = load_schedules(project_dir)

    for i, task in enumerate(schedules.get('tasks', [])):
        if task.get('id') == task_id:
            schedules['tasks'].pop(i)
            save_schedules(project_dir, schedules)
            audit_log(project_dir, 'DELETE', task_id)
            return {
                "success": True,
                "message": f"Task {task_id} removed"
            }

    return {"success": False, "error": f"Task not found: {task_id}"}


def get_logs(project_dir: Path, task_id: str, lines: int = 50) -> dict:
    """Get execution logs for a task."""
    log_file = project_dir / LOGS_DIR / f"{task_id}.log"

    if not log_file.exists():
        return {"success": False, "error": f"No logs found for task: {task_id}"}

    content = log_file.read_text()
    log_lines = content.splitlines()

    # Return last N lines
    if len(log_lines) > lines:
        log_lines = log_lines[-lines:]

    return {
        "success": True,
        "logs": "\n".join(log_lines),
        "file": str(log_file)
    }


def sync_scheduler(project_dir: Path) -> dict:
    """Sync schedules to system scheduler."""
    sync_script = Path(__file__).parent / "sync-schedules.py"

    if not sync_script.exists():
        return {"success": False, "error": "sync-schedules.py not found"}

    result = subprocess.run(
        [sys.executable, str(sync_script), "--project-dir", str(project_dir)],
        capture_output=True,
        text=True
    )

    if result.returncode != 0:
        return {
            "success": False,
            "error": f"Sync failed: {result.stderr}"
        }

    try:
        output = json.loads(result.stdout)
        audit_log(project_dir, 'SYNC', '-', f"method={output.get('method')} tasks={output.get('task_count')}")
        return output
    except json.JSONDecodeError:
        return {"success": True, "message": result.stdout}


def main():
    parser = argparse.ArgumentParser(description="Claude Code scheduler management")
    parser.add_argument("--project-dir", type=Path, default=None,
                       help="Project directory (default: current directory)")

    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # create
    create_parser = subparsers.add_parser("create", help="Create a scheduled task")
    create_parser.add_argument("time", help="Time expression (e.g., 'Every day at 9am')")
    create_parser.add_argument("prompt", help="Command/prompt to execute")
    create_parser.add_argument("--name", help="Task name")
    create_parser.add_argument("--allow", help="Comma-separated list of allowed tools")

    # list
    subparsers.add_parser("list", help="List all tasks")

    # show
    show_parser = subparsers.add_parser("show", help="Show task details")
    show_parser.add_argument("task_id", help="Task ID")

    # remove
    remove_parser = subparsers.add_parser("remove", help="Remove a task")
    remove_parser.add_argument("task_id", help="Task ID")

    # enable
    enable_parser = subparsers.add_parser("enable", help="Enable a task")
    enable_parser.add_argument("task_id", help="Task ID")

    # disable
    disable_parser = subparsers.add_parser("disable", help="Disable a task")
    disable_parser.add_argument("task_id", help="Task ID")

    # logs
    logs_parser = subparsers.add_parser("logs", help="View task logs")
    logs_parser.add_argument("task_id", help="Task ID")
    logs_parser.add_argument("--lines", type=int, default=50, help="Number of lines to show")

    # sync
    subparsers.add_parser("sync", help="Sync to system scheduler")

    args = parser.parse_args()

    project_dir = args.project_dir or get_project_dir()

    if args.command == "create":
        allowed_tools = None
        if args.allow:
            allowed_tools = [t.strip() for t in args.allow.split(',')]
        result = create_task(project_dir, args.time, args.prompt, allowed_tools, args.name)
    elif args.command == "list":
        result = list_tasks(project_dir)
    elif args.command == "show":
        result = show_task(project_dir, args.task_id)
    elif args.command == "remove":
        result = remove_task(project_dir, args.task_id)
    elif args.command == "enable":
        result = update_task(project_dir, args.task_id, enabled=True)
    elif args.command == "disable":
        result = update_task(project_dir, args.task_id, enabled=False)
    elif args.command == "logs":
        result = get_logs(project_dir, args.task_id, args.lines)
    elif args.command == "sync":
        result = sync_scheduler(project_dir)
    else:
        parser.print_help()
        sys.exit(0)

    # Output result
    if result.get('message'):
        print(result['message'])
    elif result.get('logs'):
        print(result['logs'])
    elif result.get('task'):
        print(json.dumps(result['task'], indent=2))
    else:
        print(json.dumps(result, indent=2))

    sys.exit(0 if result.get('success', False) else 1)


if __name__ == "__main__":
    main()
