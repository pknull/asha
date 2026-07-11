# Memory Operations Module — Session & Context Management

**Applies when**: Saving sessions, updating Memory files, synthesizing context, or maintaining framework documentation.

> For the full picture — the three memory scopes, how reads/writes flow, where the
> OKF learnings bundle fits, and **how to judge whether memory is providing value** —
> see [`docs/memory-architecture.md`](../../../docs/memory-architecture.md).

---

## Session Watching & Synthesis

**System**: Automated via hooks and slash commands (see `techEnvironment.md` for paths)

### Session Capture (automatic)

- Operations progressively logged to session file
- Marker overrides disable capture (see `techEnvironment.md` for marker paths)
- Captures: agent deployments, file modifications, decisions, errors

### Session Synthesis (manual via `/session:save` command)

- Four Questions Protocol guides Memory updates
- `activeContext.md` updated with session summary
- Errors noted in session archive
- Session archived

---

## Partnership Rituals

Session continuity acknowledgment via haiku:

- **Session open**: Generate haiku after Memory access (Phase 2 completion)
- **Session close**: Generate closing haiku during `/session:save` synthesis

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

**Management**: Via session capture hooks and `/session:save` command

**Frontmatter Schema** (required for all Memory/*.md):

- version, lastUpdated, lifecycle, stakeholder
- changeTrigger, validatedBy, dependencies

### B1 Extended Schema (Init 2 — active as of 2026-04-18)

Two new frontmatter fields added by B1 migration:

```yaml
type: persona | human | project | feedback | operational | reference
superseded_by: null | <filename>
```

**`type:` field** — Classifies memory file purpose:

| Value | Files | Loaded at |
|-------|-------|-----------|
| `persona` | soul.md, voice.md, keeper-voice.md | When `ASHA_PERSONA=1` |
| `human` | keeper.md, creatorProfile.md | Always |
| `project` | activeContext.md, projectbrief.md, project_*.md | Always |
| `feedback` | feedback_*.md | Always |
| `operational` | operation.md, memory-ops.md, agent-coordination.md | Always |
| `learning` | learnings/*.md (OKF bundle) | Hot tier at session start |
| `reference` | reference_*.md | On demand |

Rules:
- Every `.md` file in `~/.asha/`, `Memory/`, and the auto-memory store must have `type:`.
- Validate: `grep -rL "^type:" ~/.asha/*.md Memory/*.md` should return nothing.

**`superseded_by:` field** — Pointer to the file that replaces this one:

- Set to `null` when active.
- Set to `<filename>` when a newer file makes this one obsolete.
- **Never delete superseded files** — they are the audit trail.
- The consolidation pass in `/session:save` populates this field; humans may also set it manually.

```yaml
# Active file
type: feedback
superseded_by: null

# Superseded file (old version — keep, do not delete)
type: feedback
superseded_by: feedback_gws_over_mcp_v2.md
```

### Hot/Cold tier (learnings bundle)

Learnings live as an OKF concept bundle at `~/.asha/learnings/` — one file per
learning (`type: learning`), deduped by id, managed by `learnings_manager.py`.
Tier is **derived from confidence**, not stored as separate files:

| Tier | Selected by | Loaded at |
|------|-------------|-----------|
| Hot | Top ≤10 entries with Confidence ≥ 0.7 (`render-hot`) | Every session start (byte-budgeted) |
| Cold | Everything else | Stays in the bundle; not auto-injected |

**Threshold**: Confidence ≥ 0.7 = hot. **No manual promotion/demotion or char
budget** — selection happens at read time via `learnings_manager.py render-hot`.
Frontmatter carries an advisory `tier:` field, but the selector recomputes from
confidence. Validate the bundle with `validate.py ~/.asha/learnings`; an
auto-generated `index.md` lists every concept.

**Consolidation**: `/session:save` consolidation pass (Step C3) handles automatic promotion/demotion. See `save.md` appendix.

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
