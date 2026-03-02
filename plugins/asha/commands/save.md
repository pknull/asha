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

- Personal preferences: timezone, working style, expertise areas
- Relationship notes: what works, what doesn't
- Working patterns: how they like to receive information

**If signals found**, append to `~/.asha/keeper.md`:

1. Add timestamped entry to Calibration Log section
2. Update relevant section (Working Style, Notes)
3. Format: `YYYY-MM-DDTHH:MM:SS | project-name | "signal captured"`

**keeper.md is additive** — never overwrite, only append. Signals accumulate across sessions and projects.

Skip this step if no keeper-level signals occurred this session.

### Step 2c: Voice Calibration (Cross-Project)

Check session for voice/tone calibration signals that should update `~/.asha/voice.md`:

**Signals to capture**:

- Tone adjustments: "too much whimsy", "be more direct", "less formal"
- Pattern changes: prohibited phrases, required patterns
- Context-specific tone: how to sound during technical vs creative work

**If voice signals found**, update `~/.asha/voice.md`:

1. Update the relevant section (Voice Constraints, Communication Patterns, Context-Sensitive Tone)
2. Add note to Calibration section with timestamp and source

**Example**:

```markdown
## Voice Constraints
**PROHIBITED**:
- Whimsy during debugging (2026-01-29, threshold)
```

Skip this step if no voice calibration signals occurred this session.

### Step 2d: Session Learnings (Cross-Project)

Review the session for insights, patterns learned, or "now I know" moments:

**Questions to consider**:

- What worked well that should be repeated?
- What failed that seemed reasonable at the time?
- What tool usage patterns were discovered?
- What assumptions were validated or invalidated?

**If insights identified**, append to `~/.asha/learnings.md`:

1. Distill to a single-line pattern (not narrative)
2. Add under the appropriate category (Tool Usage, Memory Systems, Hooks, etc.)
3. Format: `- Brief description of what not to do — why / what to do instead`

**Example**:

```markdown
## Tool Usage
- Don't use `ollama run` CLI for large inputs — use HTTP API with num_predict cap
```

**learnings.md is additive** — insights accumulate. Future sessions consult this file at startup.

Skip this step if no new insights emerged this session.

### Step 3: Archive and Commit

After Memory updates are complete, run:

```bash
"${CLAUDE_PLUGIN_ROOT}/tools/save-session.sh" --archive-only
```

This will:

- Archive the session event log
- Rotate old events (keep last 30 days)

Then commit (and push if remote exists):

```bash
git add Memory/
git commit -m "Session save: <brief summary>"
git remote -v | grep -q . && git push || echo "No remote configured, skipping push"
```

## Completion Validation

If TodoWrite tasks exist, review completion:

- [ ] Goals fully achieved (not partially)
- [ ] Deliverables tested/validated
- [ ] Documentation updated
- [ ] No critical blockers remaining

Update TodoWrite: Mark truly complete tasks as completed; refine incomplete tasks.
