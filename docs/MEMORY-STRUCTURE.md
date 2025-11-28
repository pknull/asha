# Memory File Structure Specification

## Overview

Memory files provide persistent context across multi-session AI projects. This document specifies the structure, frontmatter schema, and file organization for Memory systems.

## Architectural Model

Memory systems operate on a **Core/Learning distinction**:

| Layer | Mutability | Location | Purpose |
|-------|------------|----------|---------|
| **Core** | Immutable | `Memory/*.md` | Identity, persona, protocols, project foundation |
| **Learning** | Mutable | `Work/`, `Memory/sessions/` | Session data, ephemeral context, conversation history |

**Core (Immutable)**: Files that define *who the agent is* and *how it operates*. Changes are deliberate and versioned. Includes: communicationStyle.md (persona anchor), projectbrief.md (scope foundation), workflowProtocols.md (behavioral patterns).

**Learning (Mutable)**: Files that capture *what happened* during sessions. Expected to change frequently, archived periodically. Includes: session watching logs, Work/ directory contents, ephemeral project files.

This separation enables identity persistence across sessions while allowing conversation context to evolve. The Core layer provides the "semantic lock" that prevents persona drift; the Learning layer enables adaptation without corrupting foundational identity.

## Directory Structure

```
Memory/
├── activeContext.md         # Required: Current state, recent activities, next steps
├── projectbrief.md          # Required: Scope, objectives, constraints
├── communicationStyle.md    # Required: Voice, persona, tone
├── workflowProtocols.md     # Optional: Project-specific patterns
├── techEnvironment.md       # Optional: Stack conventions, discovered patterns
├── productContext.md        # Optional: Product details
└── [custom].md              # Optional: Project-specific Memory files
```

## Required Files

### activeContext.md

**Purpose**: Session-to-session continuity - what's happening NOW

**Content**:
- **Current Project Status**: Active focus, ongoing work
- **Recent Activities**: Last 2-3 sessions' accomplishments (archive older)
- **Critical Reference Information**: Operational protocols, recurring patterns
- **Next Steps**: Immediate, monthly, deferred tasks
- **When Session Occurs**: Event-specific protocols

**Update Frequency**: Every session (via `/save`)

**Lifecycle**: Archive historical activities when >500 lines

### projectbrief.md

**Purpose**: Foundation context - what's the GOAL

**Content**:
- **Project Overview**: What are we building?
- **Scope**: What's in/out of scope
- **Objectives**: Success criteria
- **Constraints**: Technical, timeline, resource limits
- **Stakeholders**: Who cares about this project

**Update Frequency**: Rarely (major scope changes only)

**Lifecycle**: Stable foundation, minimal changes

### communicationStyle.md

**Purpose**: Voice and persona - HOW to communicate

**Content**:
- **Persona**: Character, voice, tone
- **Communication Patterns**: How to present information
- **Audience**: Who are we talking to?
- **Examples**: Concrete voice samples

**Update Frequency**: Occasionally (persona refinements)

**Lifecycle**: Evolves slowly with project maturity

## Optional Files

### workflowProtocols.md

**Purpose**: Project-specific patterns and methodologies

**Content**:
- **Workflow Patterns**: How we do recurring tasks
- **Tool Usage**: Project-specific tool conventions
- **Process Documentation**: Multi-step procedures
- **Integration Patterns**: How components interact

**When to Create**: When patterns emerge across multiple sessions

### techEnvironment.md

**Purpose**: Technical stack and discovered conventions

**Content**:
- **Stack**: Languages, frameworks, libraries, tools
- **Code Conventions**: Naming patterns, import styles, file structure
- **Build System**: How to run, test, deploy
- **Discovered Patterns**: Conventions learned from codebase inspection

**When to Create**: Software development projects

**Convention Discovery Protocol**:
1. When reading code files, note conventions (naming, imports, style)
2. Document in techEnvironment.md for consistent application
3. Check Memory before writing code (apply documented conventions)
4. Update as new patterns discovered

### productContext.md

**Purpose**: Product-specific details for non-technical stakeholders

**Content**:
- **Product Vision**: What problem are we solving?
- **User Stories**: Who uses this and why?
- **Market Context**: Competitive landscape
- **Business Logic**: Domain-specific rules

**When to Create**: Product development with business stakeholders

### Custom Files

Create project-specific Memory files as needed:
- `agentCoverageTest.md` - Agent inventory validation rubric
- `systemMonitoring.md` - Error pattern tracking
- `wireframeReference.md` - Framework evolution notes

**Naming**: Descriptive, camelCase, `.md` extension

## Frontmatter Schema

All Memory files MUST include standardized frontmatter:

```yaml
---
version: "X.Y"
lastUpdated: "YYYY-MM-DD HH:MM UTC"
lifecycle: "initiation|planning|execution|maintenance"
stakeholder: "technical|business|regulatory|all"
changeTrigger: "≥25% code impact|pattern discovery|user request|context ambiguity"
validatedBy: "human|ai|system"
dependencies: ["file1.md", "file2.md"]
---
```

### Field Definitions

**version** (required):
- Format: "X.Y" (major.minor)
- Increment minor for content updates
- Increment major for structural changes
- Example: "22.59"

**lastUpdated** (required):
- Format: "YYYY-MM-DD HH:MM UTC"
- Update on every file modification
- Example: "2025-11-15 12:30 UTC"

**lifecycle** (required):
- Values: `initiation` | `planning` | `execution` | `maintenance`
- Indicates project phase
- Helps filter relevant Memory files

**stakeholder** (required):
- Values: `technical` | `business` | `regulatory` | `all`
- Who cares about this file's content
- Enables audience-specific filtering

**changeTrigger** (required):
- What triggers updates to this file
- Common values: `≥25% code impact`, `pattern discovery`, `user request`, `context ambiguity`
- Documents when to update vs when to skip

**validatedBy** (required):
- Values: `human` | `ai` | `system`
- Who last verified content accuracy
- Tracks validation responsibility

**dependencies** (optional):
- Array of related Memory file names
- Documents file interdependencies
- Example: `["projectbrief.md", "workflowProtocols.md"]`

## File Interdependencies

**Foundation Files** (always read first):
1. activeContext.md → Depends on: projectbrief.md, communicationStyle.md
2. projectbrief.md → Standalone
3. communicationStyle.md → Standalone

**Conditional Files** (read when triggered):
- workflowProtocols.md → Depends on: projectbrief.md
- techEnvironment.md → Depends on: projectbrief.md
- productContext.md → Depends on: projectbrief.md

**Rule**: Document dependencies in frontmatter for explicit relationships

## Self-Contained Principle

**Critical Architectural Rule**:
- Memory files MUST be self-contained
- Memory files MUST NOT reference framework (AGENTS.md)
- Framework MAY reference Memory files
- This enables framework portability

**Why**:
- Framework (AGENTS.md) can be copied to new projects
- Replace Memory/ files with new project context
- Framework operates identically
- No circular dependencies

**Example Violation** (DO NOT):
```markdown
## Memory Access Protocol
See AGENTS.md I-INPUT section for reading strategy
```

**Correct Approach** (DO):
```markdown
## Memory Access Protocol
Framework reads foundation files (activeContext, projectbrief, communicationStyle) at session start.
```

## Update Triggers

Update Memory files when:
- **≥25% code impact**: Major refactoring, architectural changes
- **Pattern discovery**: New insights about project/domain
- **User request**: Explicit instruction to document
- **Context ambiguity**: Gaps causing confusion across sessions

**Do NOT update** for:
- Trivial changes (typo fixes, formatting)
- Temporary context (single-session concerns)
- Redundant information (already documented elsewhere)

## Archive Strategy

**activeContext.md Archiving**:
- When file exceeds ~500 lines, archive historical activities
- Keep only last 2-3 sessions in "Recent Activities"
- Move older activities to git history (commit messages preserve)
- Maintain "Critical Reference Information" section intact

**Session Archive**:
- Session watching files archived to `Memory/sessions/archive/`
- Named: `session-[timestamp].md`
- **Git-tracked** (permanent historical record)
- Kept for reference, not loaded automatically by framework
- Provides evidence trail of how Memory evolved

## Validation

**Frontmatter Validation**:
- All required fields present
- Values match specified enums
- Dependencies reference existing files
- Version incremented when content changes

**Content Validation**:
- No circular references to framework
- File serves documented purpose
- Update triggers appropriate
- Dependencies declared

**Size Limits**:
- activeContext.md: ~500 lines before archiving
- Session watching: 3000 lines maximum
- Other Memory files: No hard limit (reasonable size)

## Examples

See reference implementation:
- `/home/pknull/Obsidian/AAS/Memory/` (AAS project example)
- Foundation files demonstrate minimal viable structure
- Optional files show when to extend
