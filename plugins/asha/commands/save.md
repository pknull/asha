---
description: "Save current session context to Memory Bank with git commit and push"
argument-hint: "Optional: --react (include pattern analysis) or commit message details"
allowed-tools: ["Bash", "Read", "Edit", "Write", "TodoWrite"]
---

# Save Session Context

Systematic session completion protocol using the Four Questions framework.

Additional context: $ARGUMENTS

## Protocol

### Step 1: Get Session Summary

Run the save-session script to extract session activity:

```bash
"${CLAUDE_PLUGIN_ROOT}/tools/save-session.sh" --interactive
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

**Memory/scratchpad.md** (review and migrate):
- Check for notes captured via `/asha:note`
- Migrate important items to appropriate Memory files
- Prune completed or obsolete notes

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

### Step 2b: Keeper Signal Extraction (Cross-Project)

Check session for calibration signals about The Keeper that should persist across projects:

**Signals to capture**:
- Voice calibration: "too much whimsy", "be more direct", "less formal"
- Personal preferences: timezone, working style, expertise areas
- Relationship notes: what works, what doesn't

**If signals found**, append to `~/.asha/keeper.md`:
1. Add timestamped entry to Calibration Log section
2. Update relevant section (Voice Calibration, Working Style, Notes)
3. Format: `YYYY-MM-DDTHH:MM:SS | project-name | "signal captured"`

**keeper.md is additive** — never overwrite, only append. Signals accumulate across sessions and projects.

**Example update**:
```markdown
## Calibration Log
...
2026-01-29T15:30:00+10:00 | threshold | "reduce whimsy when debugging"
```

Skip this step if no keeper-level signals occurred this session.

### Step 3: Archive, Index, and Commit

After Memory updates are complete, run:

```bash
"${CLAUDE_PLUGIN_ROOT}/tools/save-session.sh" --archive-only
```

This will:
- Archive the session watching file
- Reset watching file for next session
- Refresh vector DB index (incremental)

Then commit (and push if remote exists):

```bash
git add Memory/
git commit -m "Session save: <brief summary>"
git remote -v | grep -q . && git push || echo "No remote configured, skipping push"
```

## ReAct Analysis (--react flag)

If `--react` is specified in arguments, run pattern analysis before Step 2:

```bash
"${CLAUDE_PLUGIN_ROOT}/tools/save-session.sh" --analyze
```

This provides:
- Code pattern detection and repetitions
- Redundancies with existing memory
- Novel insights extraction
- Abstraction and refactoring opportunities
- Cross-project sharing suggestions

Use insights to enhance Memory updates in Step 2.

## Completion Validation

If TodoWrite tasks exist, review completion:
- [ ] Goals fully achieved (not partially)
- [ ] Deliverables tested/validated
- [ ] Documentation updated
- [ ] No critical blockers remaining

Update TodoWrite: Mark truly complete tasks as completed; refine incomplete tasks.
