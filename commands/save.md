---
description: "Save current session context to Memory Bank with git commit and push"
argument-hint: "Optional: commit message details"
allowed-tools: ["Bash", "Read", "Edit", "Write", "TodoWrite"]
---

# Save Session Context

Systematic session completion protocol using the Four Questions framework.

Additional context: $ARGUMENTS

## Protocol

### Step 1: Get Session Summary

Run the save-session script to extract session activity:

```bash
./asha/tools/save-session.sh --interactive
```

This displays:
- Significant operations (agents invoked, files modified, panels convened)
- Decisions and clarifications made
- The Four Questions framework prompts

If no session watching file exists, proceed to Step 3 (git commit only).

### Step 2: Answer Four Questions & Update Memory

Based on the session summary, update Memory Bank files:

**Memory/activeContext.md** (always update):
- Add session summary with timestamp
- Record accomplishments
- Note key learnings
- Update Next Steps section
- Increment version number in frontmatter

**Memory/workflowProtocols.md** (if patterns learned):
- Add validated techniques
- Document pitfalls with prevention

**Memory/progress.md** (if significant milestones):
- Record phase completion
- Update project status

**If activeContext.md exceeds ~500 lines**:
- Preserve: Frontmatter, Current Status, Last 2-3 activities, Next Steps
- Archive older activities
- Target: ~150-300 lines

### Step 3: Archive, Index, and Commit

After Memory updates are complete, run:

```bash
./asha/tools/save-session.sh --archive-only
```

This will:
- Archive the session watching file
- Reset watching file for next session
- Refresh vector DB index (incremental)

Then commit and push:

```bash
git add Memory/
git commit -m "Session save: <brief summary>"
git push
```

## Completion Validation

If TodoWrite tasks exist, review completion:
- [ ] Goals fully achieved (not partially)
- [ ] Deliverables tested/validated
- [ ] Documentation updated
- [ ] No critical blockers remaining

Update TodoWrite: Mark truly complete tasks as completed; refine incomplete tasks.
