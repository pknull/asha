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
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Constants
CRONTAB_MARKER = "# CLAUDE-MANAGED-TASKS"
CRONTAB_END = "# END-CLAUDE-MANAGED-TASKS"
SYSTEMD_USER_DIR = Path.home() / ".config/systemd/user"
BACKUP_DIR = Path.home() / ".claude/backups"


def detect_scheduler() -> str:
    """Detect available scheduler: systemd or cron."""
    # Check for systemd user session
    try:
        result = subprocess.run(
            ["systemctl", "--user", "status"],
            capture_output=True,
            timeout=5
        )
        if result.returncode in (0, 3):  # 0=running, 3=no units active
            return "systemd"
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    # Check for cron
    try:
        subprocess.run(["crontab", "-l"], capture_output=True, timeout=5)
        return "cron"
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    return "none"


def validate_cron_expression(expr: str) -> bool:
    """Validate cron expression syntax (5 fields)."""
    parts = expr.split()
    if len(parts) != 5:
        return False

    # Basic validation for each field
    for i, part in enumerate(parts):
        # Handle wildcards
        if part == "*":
            continue
        # Handle step values
        if "/" in part:
            base, step = part.split("/", 1)
            if not step.isdigit():
                return False
            part = base
        # Handle ranges
        if "-" in part:
            try:
                start, end = part.split("-", 1)
                if not (start.isdigit() and end.isdigit()):
                    return False
            except ValueError:
                return False
            continue
        # Handle lists
        for segment in part.split(","):
            if segment != "*" and not segment.isdigit():
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
    """Backup current crontab before modification."""
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


def get_task_runner_path() -> str:
    """Get path to task-runner.sh script."""
    # Check common locations
    locations = [
        Path(__file__).parent / "task-runner.sh",
        Path.home() / ".claude/plugins/cache/asha-marketplace/schedule/tools/task-runner.sh",
    ]
    for loc in locations:
        if loc.exists():
            return str(loc)

    # Fallback to inline execution
    return None


def sync_to_cron(tasks: list, project_dir: Path, dry_run: bool = False) -> dict:
    """Sync tasks to user crontab."""
    # Backup first
    if not dry_run:
        backup_path = backup_crontab()

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

    # Filter to enabled tasks only
    enabled_tasks = [t for t in tasks if t.get("enabled", True)]

    # Add new CLAUDE-MANAGED section
    if enabled_tasks:
        new_lines.append("")
        new_lines.append(CRONTAB_MARKER)
        new_lines.append(f"# Generated: {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}")
        new_lines.append(f"# Project: {project_dir}")
        new_lines.append(f"# Tasks: {len(enabled_tasks)}")

        task_runner = get_task_runner_path()

        for task in enabled_tasks:
            tools = ",".join(task.get("permissions", {}).get("allowedTools", ["Read", "Grep", "Glob"]))
            timeout = task.get("timeout", 300)
            work_dir = task.get("workingDirectory", str(project_dir))
            log_file = project_dir / f".claude/logs/{task['id']}.log"

            # Ensure log directory exists
            log_file.parent.mkdir(parents=True, exist_ok=True)

            if task_runner:
                cmd = f'{task_runner} "{task["id"]}" "{work_dir}" "{task["command"]}" "{tools}" {timeout}'
            else:
                # Inline execution
                cmd = (
                    f'cd "{work_dir}" && '
                    f'timeout {timeout} claude -p "{task["command"]}" '
                    f'--allowedTools "{tools}" '
                    f'>> "{log_file}" 2>&1'
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
            "task_count": len(enabled_tasks)
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
        "task_count": len(enabled_tasks),
        "backup": str(backup_path) if backup_path else None
    }


def cron_to_oncalendar(cron_expr: str) -> str:
    """Convert cron expression to systemd OnCalendar format."""
    parts = cron_expr.split()
    if len(parts) != 5:
        return "*-*-* *:*:00"

    minute, hour, dom, month, dow = parts

    # Handle common shorthand patterns
    if cron_expr == "0 * * * *":
        return "hourly"
    if cron_expr == "0 0 * * *":
        return "daily"
    if cron_expr == "0 0 * * 0":
        return "weekly"
    if cron_expr == "0 0 1 * *":
        return "monthly"

    # Map day of week
    dow_map = {"0": "Sun", "1": "Mon", "2": "Tue", "3": "Wed",
               "4": "Thu", "5": "Fri", "6": "Sat", "7": "Sun"}

    result_dow = ""
    if dow != "*":
        if "-" in dow:
            start, end = dow.split("-")
            result_dow = f"{dow_map.get(start, start)}-{dow_map.get(end, end)} "
        else:
            result_dow = f"{dow_map.get(dow, dow)} "

    # Build date and time parts
    result_date = f"*-{month if month != '*' else '*'}-{dom if dom != '*' else '*'}"

    # Handle minute/hour with zero-padding
    h = hour if hour != '*' else '*'
    m = minute if minute != '*' else '*'

    # Handle step values in minute
    if m.startswith("*/"):
        m = f"*/{m[2:]}"
    elif m.isdigit():
        m = m.zfill(2)

    if h.isdigit():
        h = h.zfill(2)

    result_time = f"{h}:{m}:00"

    return f"{result_dow}{result_date} {result_time}".strip()


def sync_to_systemd(tasks: list, project_dir: Path, dry_run: bool = False) -> dict:
    """Sync tasks to systemd user timers."""
    SYSTEMD_USER_DIR.mkdir(parents=True, exist_ok=True)

    # Track what we create
    created = []
    errors = []

    # Remove existing claude-task-* units for this project
    project_hash = hex(hash(str(project_dir)) & 0xFFFFFFFF)[2:]
    for f in SYSTEMD_USER_DIR.glob(f"claude-task-{project_hash}-*.service"):
        if not dry_run:
            timer_file = f.with_suffix(".timer")
            if timer_file.exists():
                subprocess.run(["systemctl", "--user", "disable", timer_file.name],
                             capture_output=True)
                timer_file.unlink()
            f.unlink()

    # Filter to enabled tasks
    enabled_tasks = [t for t in tasks if t.get("enabled", True)]

    for task in enabled_tasks:
        task_id = task["id"]
        unit_name = f"claude-task-{project_hash}-{task_id}"
        tools = ",".join(task.get("permissions", {}).get("allowedTools", ["Read", "Grep", "Glob"]))
        timeout = task.get("timeout", 300)
        work_dir = task.get("workingDirectory", str(project_dir))
        log_file = project_dir / f".claude/logs/{task_id}.log"

        # Generate service file
        service_content = f"""[Unit]
Description=Claude Task: {task.get('name', task_id)}
Documentation=file://{project_dir}/.claude/schedules.json

[Service]
Type=oneshot
WorkingDirectory={work_dir}
ExecStart=/bin/bash -c 'claude -p "{task["command"]}" --allowedTools "{tools}" >> "{log_file}" 2>&1'
TimeoutStartSec={timeout}
"""

        # Generate timer file
        on_calendar = cron_to_oncalendar(task["schedule"])
        timer_content = f"""[Unit]
Description=Timer for Claude Task: {task.get('name', task_id)}

[Timer]
OnCalendar={on_calendar}
Persistent=true

[Install]
WantedBy=timers.target
"""

        service_file = SYSTEMD_USER_DIR / f"{unit_name}.service"
        timer_file = SYSTEMD_USER_DIR / f"{unit_name}.timer"

        if dry_run:
            created.append({
                "task_id": task_id,
                "service": str(service_file),
                "timer": str(timer_file),
                "on_calendar": on_calendar
            })
            continue

        try:
            # Ensure log directory exists
            log_file.parent.mkdir(parents=True, exist_ok=True)

            # Write unit files
            service_file.write_text(service_content)
            timer_file.write_text(timer_content)

            # Reload and enable
            subprocess.run(["systemctl", "--user", "daemon-reload"],
                         capture_output=True, check=True)
            subprocess.run(["systemctl", "--user", "enable", timer_file.name],
                         capture_output=True, check=True)
            subprocess.run(["systemctl", "--user", "start", timer_file.name],
                         capture_output=True, check=True)

            created.append(task_id)
        except Exception as e:
            errors.append({"task_id": task_id, "error": str(e)})

    return {
        "success": len(errors) == 0,
        "method": "systemd",
        "dry_run": dry_run,
        "task_count": len(created),
        "tasks": created,
        "errors": errors if errors else None
    }


def main():
    parser = argparse.ArgumentParser(
        description="Sync Claude schedules to system scheduler"
    )
    parser.add_argument(
        "--project-dir",
        type=Path,
        default=Path.cwd(),
        help="Project directory containing .claude/schedules.json"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without making changes"
    )
    parser.add_argument(
        "--force-cron",
        action="store_true",
        help="Force use of cron even if systemd is available"
    )
    args = parser.parse_args()

    # Resolve project directory
    project_dir = args.project_dir.resolve()

    # Load schedules
    schedules = load_schedules(project_dir)
    tasks = schedules.get("tasks", [])

    if not tasks:
        print(json.dumps({"success": True, "message": "No tasks defined", "task_count": 0}))
        sys.exit(0)

    # Validate tasks
    valid_tasks = []
    for task in tasks:
        schedule = task.get("schedule", "")
        if not validate_cron_expression(schedule):
            print(f"Warning: Invalid cron expression for task {task.get('id')}: {schedule}",
                  file=sys.stderr)
            continue
        valid_tasks.append(task)

    if not valid_tasks:
        print(json.dumps({"success": True, "message": "No valid tasks", "task_count": 0}))
        sys.exit(0)

    # Detect and sync to scheduler
    scheduler = "cron" if args.force_cron else detect_scheduler()

    if scheduler == "systemd":
        result = sync_to_systemd(valid_tasks, project_dir, args.dry_run)
    elif scheduler == "cron":
        result = sync_to_cron(valid_tasks, project_dir, args.dry_run)
    else:
        result = {
            "success": False,
            "error": "No scheduler available. Install cron or enable systemd user session."
        }

    print(json.dumps(result, indent=2, default=str))
    sys.exit(0 if result.get("success") else 1)


if __name__ == "__main__":
    main()
