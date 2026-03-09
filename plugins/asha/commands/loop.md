---
description: "Start, resume, or manage autonomous agent loops with safety guardrails"
argument-hint: "<start|resume|status|stop> [loop-id] [options]"
allowed-tools:
  - Read
  - Write
  - Edit
  - Bash
  - Grep
  - Glob
  - Task
---

# /asha:loop

Manage autonomous agent loops with checkpoint tracking and safety guardrails.

## Usage

```
/asha:loop start <objective> [--max=N] [--checkpoint=N]
/asha:loop resume <loop-id>
/asha:loop status [loop-id]
/asha:loop stop <loop-id>
/asha:loop list
```

## Commands

### start

Initialize a new autonomous loop.

```
/asha:loop start "Migrate API handlers to async/await" --max=20 --checkpoint=3
```

**Required:**

- `<objective>` — Clear description of what to accomplish

**Options:**

- `--max=N` — Maximum iterations (default: 10)
- `--checkpoint=N` — Checkpoint every N iterations (default: 3)

**Before starting, you must define:**

1. Success criteria (how do we know when done?)
2. Verification command (tests to run)
3. Rollback path (git branch name)

### resume

Continue an interrupted loop from last checkpoint.

```
/asha:loop resume silent-thunder
```

Reads state from `Work/loops/<loop-id>/state.json` and continues.

### status

Show current state of loop(s).

```
/asha:loop status                 # All active loops
/asha:loop status silent-thunder  # Specific loop
```

### stop

Gracefully stop a running loop, saving final checkpoint.

```
/asha:loop stop silent-thunder
```

### list

Show all loops with their status.

```
/asha:loop list
```

## Behavior

### On Start

1. **Generate loop ID** (dictionary words: `silent-thunder`, `bright-falcon`)
2. **Create loop directory**: `Memory/loops/<loop-id>/`
3. **Verify safety gates**:
   - Tests configured in `Memory/techEnvironment.md`
   - Git branch or worktree for isolation
   - Rollback command defined
4. **Initialize state file**: `state.json`
5. **Spawn loop-operator agent** with configuration

### During Execution

Each iteration:

1. Execute task step
2. Track progress metrics
3. At checkpoint interval: save state, run verification

### On Checkpoint

Write to `Memory/loops/<loop-id>/checkpoint-N.md`:

- Iterations completed
- Files modified
- Errors encountered
- Decision: CONTINUE / PAUSE / STOP

### On Completion

Write to `Memory/loops/<loop-id>/completion.md`:

- Final status (SUCCESS / PARTIAL / FAILED)
- Accomplishments
- Remaining items (if partial)
- Total duration

## Directory Structure

```
Work/loops/
├── index.json              # All loops registry
└── <loop-id>/
    ├── state.json          # Current state
    ├── checkpoint-1.md     # First checkpoint
    ├── checkpoint-2.md     # Second checkpoint
    └── completion.md       # Final report (when done)
```

## State Schema

```json
{
  "id": "silent-thunder",
  "objective": "Migrate API handlers to async/await",
  "status": "running",
  "created": "2026-03-08T10:00:00Z",
  "lastCheckpoint": "2026-03-08T10:15:00Z",
  "config": {
    "maxIterations": 20,
    "checkpointInterval": 3,
    "verifyCommand": "npm test",
    "rollbackRef": "main"
  },
  "progress": {
    "iteration": 7,
    "completed": ["auth.ts", "users.ts", "posts.ts"],
    "remaining": ["shares.ts", "likes.ts"],
    "errors": []
  }
}
```

## Safety Escalations

Loop automatically pauses and notifies when:

- No progress for 2 consecutive checkpoints
- Same error occurs 3+ times
- Verification command fails after fix attempt
- Context approaching compaction threshold

## Examples

### Start a migration loop

```
/asha:loop start "Convert all callbacks to async/await in src/api/"

> Define success criteria:
All files in src/api/ use async/await, tests pass

> Verification command:
npm test && npm run typecheck

> Rollback branch:
main

Loop initialized: bright-falcon
Starting iteration 1...
```

### Check on running loop

```
/asha:loop status bright-falcon

Loop: bright-falcon
Status: running
Progress: 7/15 files converted
Last checkpoint: 5 minutes ago
Next checkpoint in: 2 iterations
```

### Resume after interruption

```
/asha:loop resume bright-falcon

Resuming from checkpoint-2
Last state: 6 files completed
Continuing with: comments.ts
```
