---
name: memory-maintenance
description: Provide Memory file structure guidance when Claude updates Memory files. Covers frontmatter schema, update triggers, file interdependencies, and validation.
license: MIT
---

# Memory Maintenance Skill

**Purpose**: Provide Memory file structure guidance when Claude updates Memory files

**Invocation**: Claude autonomously uses this skill when updating Memory/*.md files

---

## When to Use This Skill

This skill provides guidance for:
- Creating new Memory files
- Updating existing Memory files
- Maintaining frontmatter schema
- Understanding file interdependencies
- Determining update triggers

## Memory File Structure

### Required Files

**Memory/activeContext.md**:
- Current project status
- Recent activities (last 2-3 sessions)
- Critical reference information
- Next steps
- Update: Every session

**Memory/projectbrief.md**:
- Project overview
- Scope (in/out)
- Objectives
- Constraints
- Update: Rarely (major scope changes)

**Memory/communicationStyle.md**:
- Persona
- Communication patterns
- Audience
- Voice examples
- Update: Occasionally (persona refinements)

### Optional Files

**Memory/workflowProtocols.md**:
- Project-specific patterns
- Tool usage conventions
- Process documentation
- Create when: Patterns emerge across multiple sessions

**Memory/techEnvironment.md**:
- Stack (languages, frameworks, tools)
- Code conventions (naming, imports, style)
- Build system
- Discovered patterns from codebase
- Create when: Software development projects

**Custom Files**:
- Create project-specific Memory files as needed
- Examples: agentCoverageTest.md, wireframeReference.md

## Frontmatter Schema

All Memory files MUST include:

```yaml
---
type: "context|brief|environment|reference|learning"   # OKF top-level concept type
version: "X.Y"
lastUpdated: "YYYY-MM-DD HH:MM UTC"
lifecycle: "initiation|planning|execution|maintenance"
stakeholder: "technical|business|regulatory|all"
changeTrigger: "≥25% code impact|pattern discovery|user request|context ambiguity"
validatedBy: "human|ai|system"
dependencies: ["file1.md", "file2.md"]
---
```

**Field Requirements**:
- **type**: OKF top-level concept type. Lets the bundle be checked/graphed by `validate.py`/`visualize.py`. Add it going forward; the existing rich fields ride along as custom keys (OKF preserves unknown keys). Not back-filled in bulk, and validation is warn-only — legacy files without it are not blocked.
- **version**: Increment minor (X.Y+1) for content, major (X+1.0) for structure
- **lastUpdated**: Update on every modification
- **lifecycle**: Current project phase
- **stakeholder**: Who cares about this content
- **changeTrigger**: What triggers updates
- **validatedBy**: Who last verified accuracy
- **dependencies**: Related Memory files (optional)

## Learnings & Ideas (OKF concept bundles)

Cross-project **learnings** are NOT a single flat file. They live as an OKF concept
bundle at `~/.asha/learnings/` — one file per learning (`<slug>.md`, frontmatter
`type: learning`), managed exclusively by `learnings_manager.py`. Recording a
learning is an upsert keyed by id (create-or-update), so the same insight cannot
accumulate duplicate copies. Do not hand-edit these files during a session; use
the manager (`add`/`confirm`/`contradict`). The hot tier injected at session start
is rendered by `learnings_manager.py render-hot`; `index.md` is auto-generated.

`Memory/ideas.md` and `Memory/scratchpad.md` remain free-form, model-maintained
prose (no code touches them). When an idea matures into a durable, reusable item,
prefer giving it its own one-concept file following the same convention rather than
growing an ever-longer flat list.

## Update Triggers

Update Memory when:
- **≥25% code impact**: Major refactoring, architectural changes
- **Pattern discovery**: New insights about project/domain
- **User request**: Explicit instruction to document
- **Context ambiguity**: Gaps causing confusion

Do NOT update for:
- Trivial changes (typos, formatting)
- Temporary context (single-session)
- Redundant information

## File Interdependencies

**Foundation Files** (always read first):
1. activeContext.md
2. projectbrief.md
3. communicationStyle.md

**Conditional Files** (read when triggered):
- workflowProtocols.md
- techEnvironment.md

Document dependencies in frontmatter.

## Self-Contained Principle

**CRITICAL RULE**:
- Memory files MUST be self-contained
- Memory files MUST NOT reference framework (AGENTS.md)
- Framework MAY reference Memory files

This enables framework portability.

## Archive Strategy

**activeContext.md**:
- Archive when >500 lines
- Keep last 2-3 sessions
- Move older activities to git history

**Session Files**:
- Archive to Work/sessions/archive/
- Named: session-[timestamp].md
- Git-ignored (ephemeral)

## Convention Discovery Protocol

When reading code files:
1. Note conventions (naming, imports, style)
2. Document in Memory/techEnvironment.md
3. Check Memory before writing code
4. Apply documented conventions
5. Update as new patterns discovered

This prevents re-discovery overhead and ensures consistency.

## Validation Checklist

Before updating Memory:
- [ ] Frontmatter complete and valid
- [ ] Version incremented
- [ ] lastUpdated timestamp current
- [ ] No references to framework (AGENTS.md)
- [ ] Dependencies declared
- [ ] Update trigger appropriate
- [ ] Content serves file purpose
- [ ] Size reasonable (activeContext <500 lines)

## Examples

See reference implementation:
- Your project's `Memory/` directory
- Foundation files show minimal structure
- Optional files show when to extend

## Documentation

For complete specifications, see plugin documentation:
- `docs/MEMORY-STRUCTURE.md` - Detailed file specifications
- `docs/SESSION-CAPTURE.md` - Session watching protocol
- `docs/SESSION-SAVE.md` - Synthesis workflow
