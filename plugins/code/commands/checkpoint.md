---
description: Create or verify named progress checkpoints with jj
argument-hint: "[create|verify|list|clear] [name]"
---

# Checkpoint Command

Create named progress markers tied to jj changes with automatic verification.

## Usage

```
/checkpoint create <name>    # Verify, create jj change, log metadata
/checkpoint verify <name>    # Compare current state to checkpoint
/checkpoint list             # Show all checkpoints
/checkpoint clear            # Remove old checkpoints (keeps last 5)
```

## Create Checkpoint

1. Run verification (build, types, tests)
2. Create jj change: `jj new -m "checkpoint: <name>"`
3. Get change ID: `jj log -r @ --no-graph -T 'change_id'`
4. Log to `Work/checkpoints/index.json`:

```json
{
  "checkpoints": [
    {
      "name": "<name>",
      "change_id": "<jj-change-id>",
      "timestamp": "<ISO-8601>",
      "tests": { "passed": 45, "failed": 2, "total": 47 },
      "coverage": 82,
      "build": "pass"
    }
  ]
}
```

1. Report checkpoint created with change ID

## Verify Checkpoint

1. Read checkpoint from index by name
2. Run current verification
3. Compare using `jj diff --from <change-id>`
4. Report:

```
CHECKPOINT COMPARISON: <name>
=============================
Change ID: <change-id>
Created: <timestamp>

Files changed since checkpoint:
  M src/utils.ts
  A src/new-feature.ts

Tests: 47 → 49 (+2 passed)
Coverage: 82% → 85% (+3%)
Build: PASS
```

## List Checkpoints

```
CHECKPOINTS
===========
  core-done        (abc123) 2h ago   tests: 45/47  cov: 82%
  feature-start    (def456) 1d ago   tests: 40/40  cov: 78%
* current          (@)               tests: 49/49  cov: 85%
```

## Clear Old Checkpoints

Remove entries from index older than 5 most recent. Does not affect jj history (changes remain).

## jj Integration

- Uses `jj new` to create checkpoint changes (not git stash)
- Each checkpoint is a real jj change, visible in `jj log`
- Multiple collaborators can checkpoint independently
- Conflicts handled naturally by jj when changes are rebased

## Storage

- Index: `Work/checkpoints/index.json`
- Directory created if missing: `mkdir -p Work/checkpoints`

## Workflow Example

```
/checkpoint create "feature-start"
  [implement core logic]
/checkpoint create "core-done"
  [add tests]
/checkpoint verify "core-done"     # What changed since core?
  [refactor]
/checkpoint verify "feature-start" # Full delta from start
```
