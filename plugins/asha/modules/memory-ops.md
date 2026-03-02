# Memory Operations Module — Session & Context Management

**Applies when**: Saving sessions, updating Memory files, synthesizing context, or maintaining framework documentation.

---

## Session Watching & Synthesis

**System**: Automated via hooks and slash commands (see `techEnvironment.md` for paths)

### Session Capture (automatic)

- Operations progressively logged to session file
- Marker overrides disable capture (see `techEnvironment.md` for marker paths)
- Captures: agent deployments, file modifications, decisions, errors

### Session Synthesis (manual via `/asha:save` command)

- Four Questions Protocol guides Memory updates
- `activeContext.md` updated with session summary
- Errors noted in session archive
- Session archived

---

## Partnership Rituals

Session continuity acknowledgment via haiku:

- **Session open**: Generate haiku after Memory access (Phase 2 completion)
- **Session close**: Generate closing haiku during `/asha:save` synthesis

---

## Documentation Updates

**Triggers**:

- Code impact changes (25%+ of codebase)
- Pattern discovery
- User request
- Context ambiguity

**Process**: Full file re-read before updating any `Memory/*.md` file

---

## Memory File Maintenance

**Management**: Via session capture hooks and `/asha:save` command

**Frontmatter Schema** (required for all Memory/*.md):

- version, lastUpdated, lifecycle, stakeholder
- changeTrigger, validatedBy, dependencies

### Periodic Trimming

`activeContext.md` accumulates session history. Without trimming, "last 7 days" sections drift into weeks or months.

**Triggers** (any of):

- File exceeds 200 lines
- "Recent Activities" section contains entries older than 14 days
- Monthly maintenance check

**Process**:

1. **Synthesize patterns** — Extract learnings from older entries into a "Synthesized Patterns" section
2. **Compress sessions** — Reduce detailed logs to one-line summaries (date + outcome)
3. **Archive if needed** — Move bulk history to `Memory/sessions/` with reference line
4. **Update frontmatter** — Increment version, update lastUpdated

**Preservation priority**:

- Patterns/learnings > session summaries > detailed logs
- Reference material (cookbooks, API docs) preserved intact
- Discovered patterns always retained

---

## Error Recovery Protocol

**Applies when**: Tool failures, agent errors, or cascading failures during task execution.

**Enforcement**: Guidance-based (protocol adherence during operation)

> **Note**: Hook-based enforcement was attempted but Claude Code's `PostToolUse` hooks only fire for successful tool calls, not failures. Error tracking remains a model-followed protocol.

### Consecutive Error Threshold

| Count | Action |
|-------|--------|
| 1 | Append to context, attempt recovery |
| 2 | Append to context, try alternate approach |
| 3 | **Escalate to user** with error summary |

### Escalation Format

When third consecutive failure occurs, stop and report:

```
[ESCALATION] Consecutive failures (3) on: {task_description}
  Errors: {brief_list}
  Attempted: {recovery_attempts}
  Awaiting: User guidance
```

### Error Logging

For persistent issues affecting future sessions, note pattern in `activeContext.md` under current focus or blockers.

---

## Framework Maintenance

Session coordinator may update AGENTS.md to improve operational efficiency.

**Constraints**:

| Action | Scope |
|--------|-------|
| PRESERVE | WIREFRAME structure, core framework architecture, operational protocols |
| MODIFY | Operating procedures, templates, efficiency optimizations |
| DO NOT MODIFY | Voice/persona (belongs in `~/.asha/voice.md`) |
| DOCUMENT | Note changes in git commits + `Memory/activeContext.md` |
