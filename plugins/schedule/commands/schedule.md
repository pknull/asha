---
name: schedule
description: "Manage scheduled Claude Code tasks (cron-style)"
argument-hint: "\"time\" \"command\" | list | remove <id> | logs <id>"
allowed-tools:
  - Read
  - Write
  - Edit
  - Bash
  - Grep
  - Glob
---

# /schedule Command

Manage scheduled task execution through external schedulers (cron/systemd).

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

### Create scheduled tasks
```
/schedule "Every weekday at 9am" "Review code changes since yesterday"
/schedule "Daily at 2am" "Run test suite and report failures"
/schedule "Every Monday at 10am" "Generate weekly status report"
/schedule "Every hour" "Check for new issues"
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

### Manual sync
```
/schedule sync                    # Re-sync schedules.json to crontab/systemd
```

## Behavior

### Task Creation
1. Parse natural language time expression to cron syntax
2. Validate constraints (rate limits, command safety, working directory)
3. Generate unique task ID (adjective-noun-NNN format)
4. Write task definition to `.claude/schedules.json`
5. Trigger `sync-schedules.py` to update system scheduler
6. Confirm with next scheduled run time

### Time Expression Parsing

| Input | Cron Expression |
|-------|-----------------|
| "Every day at 9am" | `0 9 * * *` |
| "Every weekday at 9am" | `0 9 * * 1-5` |
| "Every Monday at 2pm" | `0 14 * * 1` |
| "Every hour" | `0 * * * *` |
| "Every 15 minutes" | `*/15 * * * *` |
| "Daily at midnight" | `0 0 * * *` |
| "First of every month at noon" | `0 12 1 * *` |

### Task Listing

Displays table with:
- ID, Name, Schedule (human-readable), Next Run, Last Status, Enabled

### Task Removal
1. Remove task from schedules.json
2. Trigger sync to remove from system scheduler
3. Archive logs (not deleted)

## Output Format

### List Output
```
ID                  | Schedule           | Next Run           | Status  | Enabled
--------------------|--------------------|--------------------|---------|--------
daily-review-001    | Weekdays 9am       | 2026-01-29 09:00   | success | yes
nightly-tests-002   | Daily 2am          | 2026-01-29 02:00   | failure | yes
weekly-report-003   | Mondays 10am       | 2026-02-03 10:00   | pending | yes
```

### Creation Confirmation
```
Task created: daily-review-001
  Schedule: Every weekday at 9am (0 9 * * 1-5)
  Command: Review code changes since yesterday
  Next run: 2026-01-29 09:00 UTC
  Permissions: read-only (Read, Grep, Glob)
```

## Security

### Default Permissions
New tasks default to **read-only** mode:
- Allowed tools: Read, Grep, Glob
- No file modification or command execution

### Elevated Permissions
Request explicitly:
```
/schedule "Daily at 2am" "Fix linting errors" --allow Edit,Write
/schedule "Weekly cleanup" "Archive old logs" --allow Bash
```

### Rate Limits
- Maximum 10 scheduled tasks per project
- Maximum 5 new tasks created per hour
- Maximum 1 autonomous (Bash-enabled) task per project

## Files

| Path | Purpose |
|------|---------|
| `.claude/schedules.json` | Task definitions |
| `.claude/logs/{task-id}.log` | Execution output per task |
| `.claude/logs/{task-id}.status` | Last run status JSON |
| `.claude/logs/scheduler-audit.log` | All operations audit trail |

## Error Messages

| Error | Resolution |
|-------|------------|
| "Rate limit: Max 10 tasks per project" | Remove existing tasks first |
| "Could not parse time expression" | Use format like "Every day at 9am" |
| "Similar task already exists: {id}" | Use different command or remove existing |
| "Sync failed" | Run `/schedule sync` to retry |
| "No scheduler available" | Install cron or enable systemd user timers |

## Prerequisites

- Claude Code CLI authenticated
- Python 3.8+ (for sync script)
- cron or systemd (for task execution)
- Optional: `jq` for status updates
