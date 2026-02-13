# Schedule Plugin

**Version**: 0.1.0

Scheduled task execution for Claude Code. Manage cron-style recurring tasks with natural language time expressions.

## Overview

The schedule plugin enables automated, recurring Claude Code task execution through external schedulers (cron or systemd). You define what to run and when using natural language; the plugin handles the rest.

**Architecture**: The plugin writes task definitions to JSON; external schedulers (cron/systemd) trigger execution. Claude never runs as a daemon.

## Installation

```bash
/plugin install schedule@asha-marketplace
```

### Prerequisites

- Claude Code CLI installed and authenticated
- Python 3.8+ (for sync script)
- cron or systemd (for task execution)
- Optional: `jq` (for status tracking)

## Quick Start

```bash
# Create a scheduled task
/schedule "Every weekday at 9am" "Review code changes since yesterday"

# List all tasks
/schedule list

# View execution logs
/schedule logs daily-review-001

# Remove a task
/schedule remove daily-review-001
```

## Commands

| Command | Description |
|---------|-------------|
| `/schedule "time" "command"` | Create a scheduled task |
| `/schedule list` | Show all scheduled tasks |
| `/schedule show <id>` | Show task details |
| `/schedule remove <id>` | Delete a task |
| `/schedule enable <id>` | Enable a disabled task |
| `/schedule disable <id>` | Disable without removing |
| `/schedule logs <id>` | View execution output |
| `/schedule sync` | Re-sync to system scheduler |

## Time Expressions

Natural language parsing supports common patterns:

| Expression | Cron Equivalent |
|------------|-----------------|
| "Every day at 9am" | `0 9 * * *` |
| "Every weekday at 9am" | `0 9 * * 1-5` |
| "Every Monday at 2pm" | `0 14 * * 1` |
| "Every hour" | `0 * * * *` |
| "Every 15 minutes" | `*/15 * * * *` |
| "Daily at midnight" | `0 0 * * *` |
| "First of every month" | `0 0 1 * *` |

## Security

### Default Permissions

Tasks default to **read-only** mode:

- Allowed tools: `Read`, `Grep`, `Glob`
- No file modification or shell execution

### Elevated Permissions

Request explicitly when creating:

```bash
/schedule "Daily at 2am" "Fix linting errors" --allow Edit,Write
```

### Rate Limits

- Max 10 tasks per project
- Max 5 new tasks per hour
- Max 1 autonomous (Bash-enabled) task per project

### Blocked Patterns

Commands containing dangerous patterns are rejected:

- `rm -rf`, `rm -r /`
- `mkfs.`, `dd if=`
- Fork bombs
- Pipe to shell (`curl | sh`)

## Files

| Path | Purpose |
|------|---------|
| `.claude/schedules.json` | Task definitions |
| `.claude/logs/<task-id>.log` | Execution output |
| `.claude/logs/<task-id>.status` | Last run status (JSON) |
| `.claude/logs/scheduler-audit.log` | All operations audit |

## Architecture

```
User: /schedule "Every day at 9am" "Review code"
        │
        ▼
┌──────────────────────────────────────┐
│         Scheduler Agent              │
│  - Parse natural language            │
│  - Validate constraints              │
│  - Write to schedules.json           │
│  - Trigger sync script               │
└──────────────────────────────────────┘
        │
        ▼
┌──────────────────────────────────────┐
│      .claude/schedules.json          │
│  (version-controlled, auditable)     │
└──────────────────────────────────────┘
        │
        ▼
┌──────────────────────────────────────┐
│       sync-schedules.py              │
│  - Detect platform (cron/systemd)    │
│  - Generate scheduler entries        │
│  - Update crontab or timers          │
└──────────────────────────────────────┘
        │
        ▼
┌──────────────────────────────────────┐
│   System Scheduler (cron/systemd)    │
│  At scheduled time:                  │
│  → task-runner.sh                    │
│  → claude -p "command"               │
│  → Output to logs                    │
└──────────────────────────────────────┘
```

## Task Definition Schema

```json
{
  "version": "1.0",
  "tasks": [
    {
      "id": "daily-review-001",
      "enabled": true,
      "name": "Daily Code Review",
      "schedule": "0 9 * * 1-5",
      "scheduleHuman": "Every weekday at 9am",
      "command": "Review code changes since yesterday",
      "workingDirectory": "/path/to/project",
      "timeout": 300,
      "permissions": {
        "allowedTools": ["Read", "Grep", "Glob"],
        "readOnly": true
      },
      "created": "2026-01-28T14:30:00Z",
      "lastRun": "2026-01-29T09:00:00Z",
      "lastStatus": "success"
    }
  ]
}
```

## Troubleshooting

### Task not running

1. Check scheduler is active:

   ```bash
   # For cron
   crontab -l | grep CLAUDE-MANAGED

   # For systemd
   systemctl --user list-timers | grep claude-task
   ```

2. Verify Claude CLI is available in cron environment:

   ```bash
   which claude
   ```

3. Check task logs:

   ```bash
   /schedule logs <task-id>
   ```

### Sync failed

Run manual sync with verbose output:

```bash
python3 ~/.claude/plugins/cache/.../schedule/tools/sync-schedules.py --project-dir .
```

### Permission denied

Ensure task-runner.sh is executable:

```bash
chmod +x ~/.claude/plugins/cache/.../schedule/tools/task-runner.sh
```

## Integration

### With Asha (Memory Bank)

Task executions can be logged to Memory Bank session files if the asha plugin is installed.

### With Code Plugin

Chain commands:

```bash
/schedule "Daily at 9am" "Run /code:review on recent changes"
```

### Standalone

Works independently. Only requires Claude Code CLI and system scheduler.

## Version History

### v0.1.0 (2026-01-28)

- Initial release
- Natural language time parsing
- cron and systemd support
- Rate limiting and security constraints
- Audit logging

## License

MIT
