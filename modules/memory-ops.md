# Memory Operations Module — Session & Context Management

**Applies when**: Saving sessions, updating Memory files, synthesizing context, or maintaining framework documentation.

---

## Session Watching & Synthesis

**System**: Automated via hooks and slash commands (see `techEnvironment.md` for paths)

### Session Capture (automatic)
- Operations progressively logged to session file
- Marker overrides disable capture (see `techEnvironment.md` for marker paths)
- Captures: agent deployments, file modifications, decisions, errors

### Session Synthesis (manual via `/save` command)
- Four Questions Protocol guides Memory updates
- `activeContext.md` updated with session summary
- Errors noted in session archive
- Session archived

**Full Protocols**: `docs/SESSION-CAPTURE.md`, `docs/SESSION-SAVE.md`

---

## Partnership Rituals

Session continuity acknowledgment via haiku:
- **Session open**: Generate haiku after Memory access (Phase 2 completion)
- **Session close**: Generate closing haiku during `/save` synthesis

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

**Management**: Via session capture hooks and `/save` command

**Frontmatter Schema** (required for all Memory/*.md):
- version, lastUpdated, lifecycle, stakeholder
- changeTrigger, validatedBy, dependencies

**Full Specification**: `docs/MEMORY-STRUCTURE.md`

---

## Framework Maintenance

Session coordinator may update AGENTS.md to improve operational efficiency.

**Constraints**:
| Action | Scope |
|--------|-------|
| PRESERVE | WIREFRAME structure, core framework architecture, operational protocols |
| MODIFY | Operating procedures, templates, efficiency optimizations |
| DO NOT MODIFY | Voice/persona (belongs in `Memory/communicationStyle.md`) |
| DOCUMENT | Note changes in git commits + `Memory/activeContext.md` |
