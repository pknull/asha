---
description: "Save current session context to Memory Bank with git commit and push"
argument-hint: "Optional: commit message details"
allowed-tools: ["Bash", "Read", "Edit", "Write", "TodoWrite"]
---

# Save Session Context

Systematic session completion protocol using unified save script.

Additional context: $ARGUMENTS

## Unified Save Script

This command uses the portable `save-session.sh` script to handle all session completion logic.

**Step 1: Run Interactive Save Script**

Execute the save script in interactive mode to see session summary and Four Questions guidance:

```bash
"$(git rev-parse --show-toplevel)"/Asha/tools/save-session.sh --interactive
```

This will:
- Extract and display session watching file summary (if exists)
- Show Four Questions Protocol guidance
- Check if Memory cleanup needed (>500 lines)
- Provide Memory update instructions

**Step 2: Answer Four Questions & Update Memory**

Based on the script output, systematically update Memory Bank files:

### The Four Questions

**1. What was the goal?**
- Restate original objective from conversation or Memory/activeContext.md Next Steps
- Verify alignment between goal and actual work performed

**2. What did we accomplish?**
- List concrete deliverables completed
- Identify partial vs full completions
- Note unexpected outcomes

**3. What did we learn?**
- Validated Patterns → Add to Memory/workflowProtocols.md if significant
- Pitfalls Encountered → Document prevention strategies
- Knowledge Gaps Discovered
- Assumptions Challenged

**4. What comes next?**
- Immediate next steps (next session priorities)
- Blocked items requiring external input
- Deferred decisions with rationale

### Memory Updates Required

**Memory/activeContext.md** (always update):
- Add session summary with timestamp
- Record accomplishments (Q2)
- Note key learnings (Q3)
- Update Next Steps section (Q4)
- Increment version number in frontmatter
- Update lastUpdated timestamp

**Memory/workflowProtocols.md** (if Q3 reveals patterns):
- Add validated techniques
- Document pitfalls with prevention

**Memory/progress.md** (if significant milestones):
- Record phase completion
- Update project status

**If Memory Cleanup Triggered** (activeContext.md >500 lines):
- Preserve: Frontmatter, Current Status, Last 2-3 activities, Critical Reference, Next Steps
- Archive: Older activities (>2-3 sessions old)
- Target size: ~150-300 lines

**Step 3: Archive Session Watching File**

After Memory Bank updates are complete, archive the session watching file:

```bash
"$(git rev-parse --show-toplevel)"/Asha/tools/save-session.sh --archive-only
```

This creates timestamped archive in `Memory/sessions/archive/` and resets watching file for next session.

**Step 4: Git Commit and Push**

Stage Memory changes, commit with descriptive message, and push:

```bash
git add Memory/

# Commit with session summary
git commit -m "$(cat <<'EOF'
session: [Brief description of session activities]

[Summary of key accomplishments and decisions]

[If memory cleaned: Memory cleanup: XXX→YYY lines (Z% reduction)]

🤖 Generated with [Claude Code](https://claude.com/claude-code)

Co-Authored-By: Claude <noreply@anthropic.com>
EOF
)"

git push
```

## Completion Validation

If TodoWrite tasks exist, review completion:
- [ ] Goals fully achieved (not partially)
- [ ] Deliverables tested/validated
- [ ] Documentation updated
- [ ] No critical blockers remaining

Update TodoWrite: Mark truly complete tasks as completed; refine incomplete tasks.

## Manual Alternative

If you prefer manual control without the script, you can follow the traditional Four Questions Protocol and update Memory files directly. The script is a convenience tool, not a requirement.
