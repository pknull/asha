---
description: "Manually trigger session synthesis and git commit"
argument-hint: "[--no-push] [commit message]"
allowed-tools: ["Bash", "Read", "Edit"]
---

# Save Session

Trigger synthesis now. Use when you want to checkpoint mid-session or ensure state is captured before exiting.

**Note:** Session-end hook runs synthesis automatically on clean exit. This command is for explicit mid-session saves or when you want to add a custom commit message.

## What It Does

1. **Runs pattern analyzer** — synthesizes activeContext.md from events
2. **Extracts patterns** — updates learnings.md with discovered patterns
3. **Captures calibration** — voice.md and keeper.md signals if detected
4. **Archives events** — rotates old events to archive
5. **Git commit + push** — commits Memory/ changes

## Usage

```bash
/save                           # Synthesize + commit + push
/save --no-push                 # Synthesize + commit only
/save "Completed auth feature"  # Custom commit message
```

## Execution

Run the synthesis pipeline:

```bash
"${CLAUDE_PLUGIN_ROOT}/tools/pattern_analyzer.py" synthesize --days 7
```

Then archive and rotate events:

```bash
"${CLAUDE_PLUGIN_ROOT}/tools/save-session.sh" --archive-only
```

Then commit Memory changes:

```bash
cd "$PROJECT_DIR"
git add Memory/
git commit -m "Session save: ${ARGUMENTS:-$(date -u '+%Y-%m-%d %H:%M UTC')}"
```

Push unless `--no-push` specified:

```bash
git push
```

## When to Use

- **Mid-session checkpoint** — long session, want progress saved
- **Before risky operation** — about to do something destructive
- **Custom commit message** — want descriptive message instead of auto-generated
- **Explicit calibration** — want to manually review what gets captured

## Output

Shows synthesis results:

- Events processed
- Patterns found
- Calibration signals captured
- Files updated
