# Session Save Protocol

## Overview

The `/save` command implements a systematic session completion workflow using the Four Questions Protocol to synthesize captured operations into persistent Memory Bank updates.

## Purpose

**Problem**: Session watching captures raw operations, but doesn't automatically synthesize them into Memory structure.

**Solution**: Guided synthesis process that transforms event log into actionable Memory updates.

**Benefits**:
- Systematic Memory maintenance (prevents ad-hoc updates)
- Context preservation across sessions
- Clear separation between capture (automatic) and synthesis (guided)
- Crash recovery (watching file persists until synthesis complete)

## Workflow Phases

### Phase 1: Session Review

**Automatic**: The save script displays session watching file summary

**Sections Displayed**:
- Significant Operations (agents, file mods, panels)
- Decisions & Clarifications (user choices)
- Discoveries & Patterns (insights, ACE outputs)
- Candidates for Next Steps (identified follow-ups)

**Error Review**: Errors captured in watching file available for manual review

### Phase 2: Four Questions Protocol

**Purpose**: Structured reflection to guide Memory updates

#### Question 1: What was the goal?

- Restate original objective from conversation start
- Verify alignment between goal and actual work
- Check against `Memory/activeContext.md` Next Steps

**Why This Matters**: Validates whether session accomplished intended purpose vs. scope drift

#### Question 2: What did we accomplish?

- List concrete deliverables completed
- Identify partial vs full completions
- Note unexpected outcomes
- Document deferred work

**Deliverable Types**:
- Code written/modified
- Documentation created/updated
- Problems solved
- Decisions made
- Patterns discovered

#### Question 3: What did we learn?

**Validated Patterns** → Add to `Memory/workflowProtocols.md`:
- Techniques that worked well
- Efficiency improvements
- Tool usage patterns
- Integration approaches

**Pitfalls Encountered** → Document prevention strategies:
- Mistakes made
- Edge cases discovered
- Failure modes identified
- Correction strategies

**Knowledge Gaps Discovered**:
- What we don't know
- What needs research
- External dependencies

**Assumptions Challenged**:
- What we thought was true but wasn't
- Paradigm shifts
- Requirement clarifications

#### Question 4: What comes next?

**Immediate Next Steps** (next session priorities):
- Concrete tasks ready to execute
- Dependencies now unblocked
- Follow-up from discoveries

**Blocked Items** (requiring external input):
- User decisions needed
- Missing information
- External dependencies

**Deferred Decisions** (with rationale):
- Why postponed
- What information needed
- When to revisit

### Phase 3: Memory Bank Updates

**Always Update**: `Memory/activeContext.md`

Add session summary with:
```markdown
### Session [YYYY-MM-DD HH:MM UTC] - [Brief Title]

**Accomplishments**:
- [Deliverable 1]
- [Deliverable 2]

**Key Learnings**:
- [Pattern/Pitfall/Discovery]

**Next Steps**:
- [Immediate task 1]
- [Immediate task 2]

**Blocked**:
- [Item requiring external input]

**Deferred**:
- [Decision postponed with rationale]
```

**Update frontmatter**:
- Increment `version` (minor for content, major for structure)
- Update `lastUpdated` timestamp

**Conditional Updates**:

`Memory/workflowProtocols.md` (if Q3 reveals patterns):
- Add validated techniques with examples
- Document pitfalls with prevention strategies
- Update tool usage conventions

`Memory/progress.md` (if significant milestones):
- Record phase completion
- Update project status
- Note major deliverables

`Memory/techEnvironment.md` (if code conventions discovered):
- Document naming patterns
- Library usage patterns
- Build system changes
- Stack updates

### Phase 4: Memory Cleanup Check

**Trigger**: `activeContext.md` exceeds ~500 lines

**Archive Strategy**:
- **Preserve**: Frontmatter, Current Status, Last 2-3 session activities, Critical Reference Information, Next Steps
- **Archive**: Older activities (>2-3 sessions old)
- **Target Size**: ~150-300 lines for optimal bootstrap

**What to Archive**:
- Historical session summaries (git commit messages preserve these)
- Completed tasks
- Resolved issues
- Obsolete next steps

**What to Preserve**:
- Current project status
- Active patterns and protocols
- Unresolved blockers
- Deferred decisions
- Critical reference information

### Phase 5: Session Archive

**After Memory Updates Complete**:

```bash
"${CLAUDE_PLUGIN_ROOT}"/scripts/save-session.sh --archive-only
```

**This Operation**:
- Moves `Memory/sessions/current-session.md` to `Memory/sessions/archive/session-[timestamp].md`
- Creates fresh watching file with new session ID

**Archive Structure**:
```
Memory/sessions/archive/
├── session-2025-11-05_11-15.md
├── session-2025-11-05_19-40.md
├── session-2025-11-06_08-51.md
...
```

**Git Tracking**: Archives are git-tracked (permanent historical record)

### Phase 6: Git Commit

**Stage Memory Changes**:
```bash
git add Memory/
```

**Commit Message Format**:
```
session: [Brief description of session activities]

[Summary of key accomplishments and decisions]

[If Memory cleaned: Memory cleanup: XXX→YYY lines (Z% reduction)]

🤖 Generated with [Claude Code](https://claude.com/claude-code)

Co-Authored-By: Claude <noreply@anthropic.com>
```

**Push to Remote**:
```bash
git push
```

## Completion Validation

Before marking session complete, verify:

- [ ] All Four Questions answered systematically
- [ ] Memory/activeContext.md updated with session summary
- [ ] Version incremented, timestamp updated
- [ ] Conditional Memory files updated if triggered
- [ ] Memory cleanup performed if >500 lines
- [ ] Session watching file archived
- [ ] Git commit created with descriptive message
- [ ] Changes pushed to remote

## Manual vs Automated Modes

### Interactive Mode (Default)

**Trigger**: `/save` command

**Workflow**:
1. Display session watching summary
2. Show Four Questions Protocol guidance
3. Check Memory cleanup needed
4. Provide Memory update instructions
5. User performs updates
6. User runs `--archive-only` to archive watching file
7. User commits to git

**Best For**: Sessions with significant context requiring reflection

### Automatic Mode

**Trigger**: SessionEnd hook (clean logout)

**Workflow**:
1. Archive watching file to `Memory/sessions/archive/`
2. Create fresh watching file
3. Remind user to run `/save` for synthesis

**Best For**: Clean session termination without synthesis

### Archive-Only Mode

**Trigger**: `save-session.sh --archive-only`

**Workflow**:
1. Archive current watching file
2. Reset with new session ID
3. No Memory updates

**Best For**: After manual Memory updates, final step before git commit

## Error Handling

**During Session**: Errors captured in watching file for review

**Categories**:
- **Tool Failures**: File operations, bash commands, MCP
- **Mode Confusion**: Incorrect agent/mode selection
- **Authority Overrides**: User corrections to AI claims

**Review**: Errors visible in session watching summary for pattern analysis

## Session Watching File Lifecycle

```
[Session Start]
       ↓
Create watching file (random session ID)
       ↓
Progressive capture (operations append)
       ↓
[User runs /save]
       ↓
Display summary (Phase 1)
       ↓
Four Questions guidance (Phase 2)
       ↓
User updates Memory (Phase 3)
       ↓
User runs --archive-only
       ↓
Archive to Memory/sessions/archive/
       ↓
Reset with new session ID
       ↓
[Next Session]
```

## Crash Recovery

**Scenario**: Session interrupted (crash, network loss, forced exit)

**Recovery**:
1. Next session finds `Memory/sessions/current-session.md` from previous session
2. Run `/save` to review captured operations
3. Synthesize to Memory as normal
4. Archive and commit

**Protection**: Watching file persists until explicitly archived via `/save`

## Benefits

1. **Systematic Memory Maintenance**: Four Questions Protocol prevents ad-hoc updates
2. **Context Preservation**: Structured synthesis ensures critical information captured
3. **Crash Recovery**: Watching file survives interruptions
4. **Audit Trail**: Git-tracked archives provide historical evidence
5. **Error Visibility**: Captured in session watching for review
6. **Reduced Cognitive Load**: Guided workflow vs free recall
7. **Session Continuity**: Next session begins with complete context

## Platform-Specific Notes

### Claude Code

- Full automation via hooks and bash scripts
- `${CLAUDE_PLUGIN_ROOT}` for portable paths
- Git integration automatic
- Recommended workflow: `/save` → update Memory → `--archive-only` → git commit

### Claude.ai

- Manual Four Questions Protocol
- Copy watching file content manually
- Update Memory files via Projects interface
- Manual archive (rename/move watching file)

### ChatGPT

- Conversation-based synthesis
- Export conversation as session archive
- Manual Memory file updates
- No automatic watching file

### Gemini

- Manual protocol only
- Research platform capabilities TBD

## Examples

### Example Session Completion

**Watching File Summary**:
```
## Significant Operations
- [2025-11-15 12:15] Agent: intellectual-property → Analyzed 442 files, 367 approved
- [2025-11-15 12:30] Modified: Vault/World/Bestiary/Wooden Companions.md → Created creature
- [2025-11-15 13:00] Panel: Copyright clearance → All blockers resolved

## Decisions & Clarifications
- [2025-11-15 12:20] Decision: Birthing Trees verification → Original confirmed

## Discoveries & Patterns
- [2025-11-15 12:25] Pattern: Referencing public domain deity ≠ derivative work
```

**Four Questions Answers**:
1. **Goal**: Verify copyright clearance for AAS Thorne campaign
2. **Accomplished**: IP audit complete, 3 creatures replaced, all blockers resolved
3. **Learned**: Public domain reference patterns, replacement strategies
4. **Next**: Resume game development with cleared setting

**Memory Update** (activeContext.md):
```markdown
### Session 2025-11-15 12:00 UTC - Copyright Clearance Audit

**Accomplishments**:
- IP audit via intellectual-property agent: 442 files analyzed
- Replaced 3 copyrighted creatures (Dark Young → Wooden Companions, etc)
- Copyright clearance panel consensus: 0 commercial blockers remaining

**Key Learnings**:
- Referencing public domain deity names (Shub-Niggurath) ≠ derivative work
- Environmental horror mechanics distinct from Lovecraft creature implementations
- Replacement strategy: Preserve thematic function, change mechanical implementation

**Next Steps**:
- Resume Thorne campaign development with commercially cleared AAS setting
- Deferred: 77 Lovecraft-derived description rewrites (production phase)
```

## Troubleshooting

**Issue**: Watching file missing
- **Cause**: No significant operations captured this session
- **Solution**: Skip `/save` (nothing to synthesize)

**Issue**: Memory cleanup warning shown
- **Cause**: activeContext.md >500 lines
- **Solution**: Archive older activities after synthesis

**Issue**: Errors not visible in summary
- **Cause**: Session watching file missing error section
- **Solution**: Review raw watching file for error entries

**Issue**: Archive already exists with same timestamp
- **Cause**: Multiple saves in same minute
- **Solution**: Wait 1 minute or manually rename archive file
