# CLAUDE.md - AI Assistant Guide for asha

**Version**: 1.12.0
**Last Updated**: 2026-06-11 (superseded sections noted inline)
**Repository**: pknull/asha

---

> ### ⚠ Install model has changed
>
> This repo no longer ships as a Claude plugin marketplace. The three-file
> registration chain (`marketplace.json` → `installed_plugins.json` →
> `enabledPlugins`) is deprecated. Primitives are now installed via direct
> symlinks into `~/.claude/*` by **`./install.sh`**.
>
> Launch through the unified **`asha`** dispatcher — `asha <harness>` (claude|codex|copilot),
> auto-configuring on first use. `asha-claude`/`asha-codex`/`asha-copilot` remain as
> back-compat shims. Engines live in `lib/install.sh` + `lib/uninstall.sh`; the top-level
> `install.sh`/`uninstall.sh` are thin shims.
>
> Any section below that describes `/plugin marketplace add`, plugin.json
> entry points, or marketplace.json registration is **historical** and
> retained only for context. Authoritative: **[INSTALLER.md](INSTALLER.md)**.

---

## Purpose of This Document

This guide helps AI assistants (like Claude) understand the asha codebase structure, development workflows, architectural patterns, and key conventions. Use this as your primary reference when working on this repository.

---

## Table of Contents

1. [Project Overview](#project-overview)
2. [Repository Structure](#repository-structure)
3. [Architecture & Design Philosophy](#architecture--design-philosophy)
4. [Plugin System](#plugin-system)
5. [Development Workflows](#development-workflows)
6. [Key Conventions](#key-conventions)
7. [Memory System Integration](#memory-system-integration)
8. [Testing & Validation](#testing--validation)
9. [Git Workflows](#git-workflows)
10. [Common Tasks & Patterns](#common-tasks--patterns)

---

## Project Overview

**asha** is a multi-harness agent toolkit (Claude Code, Codex, Copilot) providing tools for multi-perspective analysis, code review, output styling, and session coordination. It installs via direct symlink-mount (`./install.sh`), **not** as a plugin marketplace — see [INSTALLER.md](INSTALLER.md). Marketplace-flow sections below are historical (see the banner at top).

### Current Plugins

| Plugin | Version | Domain | Description |
|--------|---------|--------|-------------|
| **Panel System** | v5.0.0 | Research | Multi-perspective analysis with persistence and resumption |
| **Code** | v1.1.0 | Development | Code review, orchestration patterns, TDD workflows |
| **Write** | v1.2.0 | Creative | Prose craft, worldbuilding, storytelling agents |
| **Output Styles** | v1.0.2 | Formatting | Switchable response styles |
| **Asha** | v1.11.1 | Core | Session coordination, cross-project identity (~/.asha/) |
| **Image** | v1.1.0 | Creative | Stable Diffusion prompts, ComfyUI workflows |
| **Schedule** | v0.1.0 | Automation | Cron-style scheduled tasks |

### Technology Stack

- **Primary Format**: Markdown (commands, agents, documentation)
- **Scripting**: Bash (hooks, automation)
- **Configuration**: JSON, YAML frontmatter
- **Platform**: Claude Code CLI
- **Version Control**: Git

---

## Repository Structure

```
asha/
├── .claude-plugin/
│   └── marketplace.json              # Marketplace registry and plugin list
├── plugins/
│   ├── panel/                         # Research & analysis
│   │   ├── .claude-plugin/
│   │   │   └── plugin.json
│   │   ├── commands/
│   │   │   └── panel.md              # /panel command
│   │   ├── agents/
│   │   │   └── recruiter.md
│   │   └── docs/characters/          # Character profiles (3)
│   ├── code/                          # Development workflows
│   │   ├── .claude-plugin/
│   │   │   └── plugin.json
│   │   ├── commands/
│   │   │   └── review.md             # /code:review command
│   │   ├── agents/
│   │   │   └── codebase-historian.md
│   │   ├── modules/
│   │   │   ├── code.md
│   │   │   └── orchestration.md
│   │   └── recipes/                  # Multi-agent workflows
│   ├── write/                         # Creative writing
│   │   ├── .claude-plugin/
│   │   │   └── plugin.json
│   │   ├── agents/                   # 5 writing agents
│   │   ├── modules/
│   │   │   └── writing.md
│   │   └── recipes/                  # Writing workflows
│   ├── output-styles/                 # Response formatting
│   │   ├── .claude-plugin/
│   │   │   └── plugin.json
│   │   ├── commands/
│   │   │   └── style.md              # /style command
│   │   ├── hooks/
│   │   └── styles/                   # 8 style definitions
│   └── asha/                          # Core scaffold
│       ├── .claude-plugin/
│       │   └── plugin.json
│       ├── commands/                 # /asha:* commands
│       ├── hooks/
│       ├── modules/                  # Core techniques
│       │   ├── CORE.md
│       │   ├── cognitive.md
│       │   ├── research.md
│       │   ├── memory-ops.md
│       │   ├── high-stakes.md
│       │   └── verbalized-sampling.md
│       ├── templates/                # Memory Bank templates
│       └── tools/                    # Python tools
├── .gitignore
├── LICENSE (MIT)
├── README.md
└── CLAUDE.md (this file)
```

### Critical File Paths

| Path | Purpose |
|------|---------|
| `.claude-plugin/marketplace.json` | Defines available plugins and sources |
| `plugins/[name]/.claude-plugin/plugin.json` | Plugin metadata and entry points |
| `plugins/[name]/commands/*.md` | User-facing slash commands |
| `plugins/[name]/agents/*.md` | Agent definitions for deployment |
| `plugins/[name]/hooks/hooks.json` | Lifecycle hook configuration |

---

## Architecture & Design Philosophy

### Core Principles

1. **Separation of Concerns**
   - Framework (AGENTS.md) tells Claude to READ Memory
   - Plugins tell Claude HOW TO MAINTAIN Memory
   - Character files are narrative personas, not technical roles

2. **Portability First**
   - Memory files MUST be self-contained
   - Memory files MUST NOT reference framework
   - Framework MAY reference Memory files
   - Enables framework reuse across projects

3. **Multi-Session Continuity**
   - Each session begins fresh (Claude context resets)
   - Memory is the ONLY connection to previous work
   - Session watching captures operations automatically
   - Synthesis transforms operations into persistent context

4. **Character-Based Design**
   - Separate narrative personas from technical implementation
   - Characters have defined voice, appearance, role
   - Characters map to technical capabilities via agent deployments

### Plugin Integration Strategies

- **Command-Based**: Explicit user invocation (`/panel`, `/code:review`, `/style`, `/asha:save`)
- **Hook-Based**: Automatic capture (PostToolUse, UserPromptSubmit, SessionEnd)
- **Skill-Based**: Autonomous guidance (memory-maintenance)
- **Marker-Based**: Control flow via marker files (silence, rp-active)

---

## Plugin System

### Plugin Structure Standard

Every plugin follows this structure:

```
[plugin-name]/
├── .claude-plugin/
│   └── plugin.json           # Required: Plugin metadata
├── commands/                 # Optional: User-facing commands
│   └── [command].md
├── agents/                   # Optional: Agent definitions
│   └── [agent].md
├── skills/                   # Optional: Autonomous skills
│   └── [skill]/
│       └── SKILL.md
├── hooks/                    # Optional: Lifecycle hooks
│   ├── hooks.json
│   └── [hook-script]
├── scripts/                  # Optional: Utility scripts
│   └── [script].sh
├── docs/                     # Optional: Documentation
│   └── [doc].md
├── README.md                 # Required: Plugin overview
└── LICENSE                   # Required: License file
```

### Plugin Metadata Format

**File**: `.claude-plugin/plugin.json`

```json
{
  "name": "plugin-name",
  "version": "X.Y.Z",
  "description": "Brief description",
  "author": {
    "name": "Author Name",
    "email": "email@example.com"
  },
  "license": "MIT",
  "keywords": ["tag1", "tag2"],
  "commands": "./commands",
  "agents": ["./agents/agent.md"],
  "skills": "./skills",
  "hooks": "./hooks/hooks.json"
}
```

### Marketplace Registration

**File**: `.claude-plugin/marketplace.json`

```json
{
  "name": "asha",
  "owner": {"name": "pknull", "email": "noreply@example.com"},
  "metadata": {
    "description": "...",
    "version": "1.3.0",
    "homepage": "https://github.com/pknull/asha"
  },
  "plugins": [
    {
      "name": "plugin-name",
      "description": "...",
      "source": "./plugins/plugin-name",
      "strict": false
    }
  ]
}
```

---

## Development Workflows

### Adding a New Plugin

1. **Create plugin directory structure**

   ```bash
   mkdir -p plugins/[plugin-name]/{.claude-plugin,commands,agents,docs}
   ```

2. **Create plugin.json**
   - Define name, version, description, author
   - Specify entry points (commands, agents, hooks, skills)

3. **Implement functionality**
   - Commands: Markdown with optional YAML frontmatter
   - Agents: Markdown with agent definition
   - Hooks: Bash scripts + hooks.json registry
   - Skills: SKILL.md in named directory

4. **Update marketplace.json**
   - Add plugin to `plugins` array
   - Set correct source path

5. **Write documentation**
   - README.md with usage examples
   - Add LICENSE file (MIT recommended)

6. **Test installation**

   ```bash
   ./install.sh --only [plugin-name]   # or ./install.sh to (re)install all
   ```

### Modifying Existing Plugins

1. **Read existing implementation**
   - Review plugin.json for structure
   - Read command/agent/hook files
   - Check docs/ for specifications

2. **Make changes incrementally**
   - Update version in plugin.json (increment minor for content, major for structure)
   - Test each change in isolation
   - Update documentation to match

3. **Validate frontmatter**
   - Ensure YAML frontmatter is valid
   - Update `lastUpdated` timestamps
   - Increment `version` fields

4. **Test end-to-end**
   - Reinstall plugin to test loading
   - Execute commands to verify behavior
   - Check hooks trigger correctly

---

## Key Conventions

### Naming Conventions

| Element | Convention | Example |
|---------|-----------|---------|
| Memory files | camelCase | `activeContext.md`, `projectbrief.md` |
| Commands | kebab-case | `save`, `silence`, `panel` |
| Agents | kebab-case | `recruiter`, `prose-analysis` |
| Characters | Title Case | `The Moderator`, `The Analyst`, `The Challenger` |
| Scripts | kebab-case.sh | `save-session.sh`, `common.sh` |
| Session IDs | dictionary-words or hex | `silent-thunder`, `a3f8c2d1` |

### File Format Conventions

**Command Files** (`commands/*.md`):

```markdown
---
description: "Brief description"
argument-hint: "Optional: argument format"
allowed-tools: ["Tool1", "Tool2"]
---

# Command Name

## Usage
/command [arguments]

## Behavior
[Description of what command does]
```

**Agent Files** (`agents/*.md`):

```markdown
---
title: Agent Name
type: agent
domain: [domain]
---

# Agent Name

## Purpose
[What this agent does]

## Capabilities
- Capability 1
- Capability 2

## Usage
[When to deploy this agent]
```

**Character Files** (`docs/characters/*.md`):

```markdown
---
title: Character Name
type: character
status: draft
---

# Character Name

## Nature
[Conceptual essence]

## Appearance
[Presentation style]

## Voice Quality
[Communication patterns]

## Role in Panel Sessions
[Specific function]

## Capability Requirements
[Required agent deployments]
```

### Versioning Convention

**Format**: `X.Y.Z` or `X.Y`

- **Major (X)**: Breaking changes, structural refactors
- **Minor (Y)**: New features, content updates
- **Patch (Z)**: Bug fixes, typos (optional for docs)

**Examples**:

- Panel system: v5.0.0
- Memory files: v2.1 (no patch for documentation)

### Timestamp Convention

**Format**: `YYYY-MM-DD HH:MM UTC`

- Always use UTC timezone
- Used in: frontmatter, session files, archives
- Example: `2025-11-17 14:30 UTC`

### Bash Script Safety

**All scripts must include**:

```bash
#!/usr/bin/env bash
set -euo pipefail  # Exit on error, undefined vars, pipe failures

# Optional: Source shared utilities
source "$(dirname "$0")/common.sh"
```

**Error Handling Pattern**:

```bash
# Silent fallback for optional features
if ! command -v jq &>/dev/null; then
    echo "{}" >&2
    exit 0
fi

# Defensive directory creation
mkdir -p "$PROJECT_DIR/Memory/sessions"
mkdir -p "$PROJECT_DIR/Memory/markers"
```

---

## Memory System Integration

### Memory Directory Structure (User Projects)

Plugins document but don't create this structure (users create per-project):

```
Memory/
├── activeContext.md          # Required: Current session state
├── projectbrief.md           # Required: Project foundation
├── communicationStyle.md     # Required: Voice/persona
├── workflowProtocols.md      # Optional: Project patterns
├── techEnvironment.md        # Optional: Stack conventions
├── productContext.md         # Optional: Product details
├── sessions/                 # Auto-created by hooks
│   ├── current-session.md    # Auto-appended during session
│   └── archive/              # Historical sessions (git-tracked)
├── markers/                  # Auto-created by hooks
│   ├── silence              # Disable logging
│   ├── rp-active            # RP mode active
│   └── prompt-refine        # Enable LanguageTool
└── [custom].md              # Project-specific files
```

### Frontmatter Schema (All Memory Files)

```yaml
---
version: "X.Y"
lastUpdated: "YYYY-MM-DD HH:MM UTC"
lifecycle: "initiation|planning|execution|maintenance"
stakeholder: "technical|business|regulatory|all"
changeTrigger: "≥25% code impact|pattern discovery|user request|context ambiguity"
validatedBy: "human|ai|system"
dependencies: ["file1.md", "file2.md"]  # Optional
---
```

### Marker Files

| Marker | Effect | Created By | Removed By |
|--------|--------|-----------|-----------|
| `Memory/markers/silence` | Disable ALL Memory logging | `/silence on` | `/silence off` or session-end |
| `Memory/markers/rp-active` | Disable session watching | Manual | session-end hook |
| `Memory/markers/prompt-refine` | Enable LanguageTool API | Manual | Manual |

### Hook Behavior with Markers

**All hooks check markers first**:

```bash
# Exit silently if silence marker exists
if [ -f "$PROJECT_DIR/Memory/markers/silence" ]; then
    echo "{}" >&2
    exit 0
fi

# Exit silently if RP mode active (session watching only)
if [ -f "$PROJECT_DIR/Memory/markers/rp-active" ]; then
    echo "{}" >&2
    exit 0
fi
```

### Project Directory Detection (Multi-Layer Fallback)

```bash
# 1. Environment variable (hook invocation)
if [ -n "${CLAUDE_PROJECT_DIR:-}" ]; then
    PROJECT_DIR="$CLAUDE_PROJECT_DIR"

# 2. Git root with Memory/ directory (manual invocation)
elif GIT_ROOT=$(git rev-parse --show-toplevel 2>/dev/null) && [ -d "$GIT_ROOT/Memory" ]; then
    PROJECT_DIR="$GIT_ROOT"

# 3. Upward search from current directory
else
    CURRENT_DIR="$(pwd)"
    while [ "$CURRENT_DIR" != "/" ]; do
        if [ -d "$CURRENT_DIR/Memory" ]; then
            PROJECT_DIR="$CURRENT_DIR"
            break
        fi
        CURRENT_DIR="$(dirname "$CURRENT_DIR")"
    done
fi
```

---

## Testing & Validation

### Validation Checklist

**Before committing plugin changes**:

1. **Plugin Metadata**
   - [ ] plugin.json is valid JSON
   - [ ] Version incremented appropriately
   - [ ] All entry points (commands, agents, hooks) exist

2. **Frontmatter Validation**
   - [ ] All YAML frontmatter is valid
   - [ ] Required fields present (version, lastUpdated)
   - [ ] Timestamps in correct format (YYYY-MM-DD HH:MM UTC)

3. **Bash Scripts**
   - [ ] All scripts have `set -euo pipefail`
   - [ ] No undefined variables
   - [ ] Defensive directory creation (`mkdir -p`)

4. **Documentation**
   - [ ] README.md updated with changes
   - [ ] Examples reflect current behavior
   - [ ] LICENSE file present

5. **Installation Test**

   ```bash
   ./install.sh --only [plugin-name]   # symlink-mount install
   ls ~/.claude/commands ~/.claude/skills | grep [plugin-name]   # verify primitives mounted
   ```

6. **Functional Test**

   ```bash
   /[command]  # Test each command
   # Verify expected behavior
   # Check for errors in output
   ```

### No Automated Test Suite

This repository uses **manual validation** and **documentation-driven testing**:

- Character files validated against schema
- Frontmatter validated on read
- Hook JSON schema compliance checked by Claude Code
- Directory structure auto-created defensively

---

## Git Workflows

### Branch Strategy

Development occurs on feature branches:

- Branch pattern: `claude/claude-md-[session-id]-[random-id]`
- Example: `claude/claude-md-mi3ish2l1isy92na-01En42UogD6rR8J78vFWiNZu`

### Commit Message Convention

**Format**: Conventional Commits style

```
<type>: <description>

[optional body]
```

**Types**:

- `feat`: New feature
- `fix`: Bug fix
- `docs`: Documentation changes
- `refactor`: Code refactoring
- `test`: Test additions/changes
- `chore`: Maintenance tasks

**Examples**:

```
feat: Add memory-maintenance skill for autonomous guidance
fix: Move silence/rp-active markers from Work to Memory
docs: Update panel README with recruitment architecture
refactor: Consolidate marker path references
```

### Push Protocol

**Always use**:

```bash
git push -u origin <branch-name>
```

**Branch must**:

- Start with `claude/`
- End with matching session ID
- Otherwise push fails with 403 HTTP error

**Network retry logic** (on failure):

1. Wait 2s, retry
2. Wait 4s, retry
3. Wait 8s, retry
4. Wait 16s, retry
5. Give up after 4 retries

### Pull Request Workflow

1. **Ensure all changes committed**

   ```bash
   git status  # Should be clean
   ```

2. **Push to feature branch**

   ```bash
   git push -u origin <branch-name>
   ```

3. **Create PR** (via user request)
   - AI cannot use `gh` CLI (not available)
   - User creates PR manually via GitHub UI
   - Reference issue numbers if applicable

---

## Common Tasks & Patterns

### Task: Add New Command to Existing Plugin

1. **Create command file**

   ```bash
   # Location: plugins/[plugin-name]/commands/[command].md
   ```

2. **Add frontmatter** (optional)

   ```yaml
   ---
   description: "Command description"
   argument-hint: "Optional: argument format"
   allowed-tools: ["Tool1", "Tool2"]
   ---
   ```

3. **Write command documentation**
   - Usage section
   - Behavior description
   - Examples

4. **Update plugin.json**
   - If using directory: `"commands": "./commands"` (auto-discovers)
   - If using array: Add `"./commands/[command].md"` to array

5. **Test command**

   ```bash
   ./install.sh --only [plugin-name]
   /[command]
   ```

### Task: Add New Hook

1. **Create hook script**

   ```bash
   # Location: plugins/[plugin-name]/hooks/[hook-name]
   chmod +x plugins/[plugin-name]/hooks/[hook-name]
   ```

2. **Add safety headers**

   ```bash
   #!/usr/bin/env bash
   set -euo pipefail
   source "$(dirname "$0")/common.sh"
   ```

3. **Implement hook logic**
   - Check markers first (exit silently if present)
   - Detect project directory (multi-layer fallback)
   - Create directories defensively
   - Output JSON for success/failure

4. **Register in hooks.json**

   ```json
   {
     "hooks": {
       "HookName": [{
         "matcher": "*",  // Optional: filter by tool
         "hooks": [{
           "type": "command",
           "command": "${CLAUDE_PLUGIN_ROOT}/hooks/[hook-name]"
         }]
       }]
     }
   }
   ```

5. **Test hook**
   - Trigger condition (e.g., Edit file for PostToolUse)
   - Verify hook executes
   - Check expected side effects

### Task: Update Character Profile

1. **Read existing character file**

   ```bash
   # Location: plugins/panel/docs/characters/[Character].md
   ```

2. **Update sections**
   - Nature: Conceptual essence
   - Appearance: Presentation style
   - Voice Quality: Communication patterns
   - Role in Panel Sessions: Specific function
   - Capability Requirements: Required agents

3. **Preserve frontmatter**

   ```yaml
   ---
   title: Character Name
   type: character
   status: draft
   ---
   ```

4. **Update panel.md if behavior changes**
   - Character descriptions
   - Phase assignments
   - Protocol steps

### Task: Version Bump

1. **Determine version change type**
   - Major (X): Breaking changes, structural refactors
   - Minor (Y): New features, content updates
   - Patch (Z): Bug fixes, typos

2. **Update plugin.json**

   ```json
   "version": "X.Y.Z"
   ```

3. **Update marketplace.json** (if needed)

   ```json
   "metadata": {
     "version": "X.Y.Z"
   }
   ```

4. **Update documentation**
   - README.md version history
   - CLAUDE.md last updated timestamp
   - Any version references in docs

5. **Commit with version tag**

   ```bash
   git commit -m "chore: Bump version to X.Y.Z"
   git tag vX.Y.Z
   ```

### Task: Debug Hook Not Triggering

1. **Check marker files**

   ```bash
   ls -la Memory/markers/
   # Remove silence/rp-active if present
   ```

2. **Verify project directory detection**

   ```bash
   # Set environment variable explicitly
   export CLAUDE_PROJECT_DIR=$(pwd)
   ```

3. **Check hooks.json syntax**
   - Validate JSON with `jq`
   - Ensure correct matcher patterns
   - Verify command path uses `${CLAUDE_PLUGIN_ROOT}`

4. **Test hook manually**

   ```bash
   cd plugins/[plugin-name]/hooks
   CLAUDE_PROJECT_DIR=/path/to/project ./[hook-name]
   # Should output JSON: {} for success
   ```

5. **Check hook permissions**

   ```bash
   chmod +x plugins/[plugin-name]/hooks/[hook-name]
   ```

6. **Review hook output**
   - stderr messages for debugging
   - JSON stdout for Claude Code integration

---

## Best Practices for AI Assistants

### When Working on This Repository

1. **Always read before editing**
   - Use Read tool to examine existing files
   - Understand current structure before changes

2. **Preserve existing conventions**
   - Follow naming patterns (camelCase, kebab-case)
   - Maintain frontmatter structure
   - Keep timestamps in UTC format

3. **Test installation after changes**
   - Verify plugin.json is valid JSON
   - Check command/agent files exist at specified paths
   - Test end-to-end installation flow

4. **Update documentation**
   - README.md reflects current behavior
   - CLAUDE.md updated for structural changes
   - Version history maintained

5. **Commit incrementally**
   - Small, focused commits
   - Clear commit messages following convention
   - Test each change before committing

### When Reading User Requests

1. **Identify plugin scope**
   - Panel system: `/panel` command, character profiles, recruitment (Research domain)
   - Code: `/code:review` command, codebase-historian, orchestration (Development domain)
   - Write: 5 writing agents, recipes, prose craft (Creative domain)
   - Output Styles: `/style` command, response formatting
   - Asha: `/asha:*` commands, Memory Bank, core modules (Core scaffold)

2. **Check for Memory file references**
   - Memory files live in user projects, not this repo
   - This repo only documents Memory structure
   - Don't create Memory files in asha

3. **Distinguish character from implementation**
   - Characters are narrative personas (The Moderator, The Analyst, The Challenger)
   - Implementation uses agents, commands, hooks
   - Character files describe voice/role, not technical details

4. **Respect portability constraints**
   - Memory files MUST be self-contained
   - No circular references between framework and Memory
   - Plugins guide Memory maintenance, don't control it

### Common Pitfalls to Avoid

1. **Don't create Memory/ in asha**
   - Memory lives in user projects
   - This repo documents but doesn't instantiate

2. **Don't mix character and technical documentation**
   - Characters in `docs/characters/`
   - Technical specs in README.md, SKILL.md, etc.

3. **Don't break plugin.json structure**
   - Always validate JSON before committing
   - Test that paths resolve correctly
   - Use `${CLAUDE_PLUGIN_ROOT}` for hook commands

4. **Don't skip version increments**
   - Every content change = minor bump
   - Every structure change = major bump
   - Update plugin.json + marketplace.json

5. **Don't ignore marker files**
   - Silence marker = no Memory logging
   - RP-active marker = no session watching
   - Hooks exit silently if markers present

---

## Additional Resources

### Documentation Files

- `README.md`: Marketplace overview
- `plugins/panel/README.md`: Panel system documentation

### Key Configuration Files

- `.claude-plugin/marketplace.json`: Plugin registry
- `plugins/panel/.claude-plugin/plugin.json`: Panel metadata
- `plugins/code/.claude-plugin/plugin.json`: Code metadata
- `plugins/write/.claude-plugin/plugin.json`: Write metadata
- `plugins/output-styles/.claude-plugin/plugin.json`: Output Styles metadata
- `plugins/asha/.claude-plugin/plugin.json`: Asha metadata

### External References

- **Claude Code Documentation**: https://docs.claude.com/en/docs/claude-code/
- **Repository Issues**: https://github.com/pknull/asha/issues
- **MIT License**: https://opensource.org/licenses/MIT

---

## Version History

### v1.9.0 (2026-01-29)

- **Panel system v5.0.0**: Full state persistence and panel management
  - `--resume <id>`: Continue interrupted panels from last completed phase
  - `--list [--status=X]`: Query panel index with optional filtering
  - `--show <id>`: Display panel summary
  - `--abandon <id>`: Mark panels as abandoned
  - Output moved from `Work/meetings/` to `Work/panels/` with per-phase state files
  - New files: `state.json`, `index.json`, `phase-*.md`, `transcript.md`
- **Asha v1.8.0**: Cross-project identity layer
  - New `~/.asha/` directory for user-scope identity (not committed to repos)
  - `communicationStyle.md`: Who Asha is (voice, persona, constraints)
  - `keeper.md`: Who you are (calibration signals via `/save`)
  - Session-start hook auto-injects identity files from `~/.asha/`
  - `/asha:init` bootstraps both identity layer and project Memory
  - `/asha:save` captures keeper calibration signals to `~/.asha/keeper.md`

### v1.8.0 (2026-01-28)

- **New plugin: schedule** — Cron-style task automation with natural language time parsing
  - Natural language parser (20+ expressions: "Every weekday at 9am", "Every 15 minutes", etc.)
  - Task management with rate limiting, duplicate detection, dangerous command blocking
  - systemd timer and cron backend support with automatic detection
  - Execution wrapper with timeout handling, status tracking, audit logging
  - End-to-end tested: tasks execute on schedule, Claude responds correctly

### v1.7.0 (2026-01-26)

- **New plugin: image** — Image generation workflows with Stable Diffusion prompt engineering, ComfyUI workflow design
- Standards compliance audit per Claude Code skills best practices
- Fixed hardcoded paths, added frontmatter to agent files
- All plugin versions incremented for upgrade path

### v1.6.0 (2026-01-26)

- **Domain restructuring**: Organized plugins by workflow type (panel=research, code=dev, write=creative, asha=core)
- **New plugin: code** — Development workflows with codebase-historian agent, orchestration patterns, quality gates, swarm recipes
- **New plugin: write** — Creative writing with 5 specialized agents (outline-architect, prose-writer, consistency-checker, developmental-editor, line-editor) and recipes
- **Absorbed local-review** into code plugin as `/code:review`
- **ACE cycle moved** to asha/modules/cognitive.md as general technique
- Cleaned up asha to core scaffold only (moved domain content to code/write)

### v1.5.0 (2026-01-16)

- Fixed hook handler permissions (711 → 755) and naming consistency (added .sh extensions)
- Added version validation script (tests/validate-versions.sh)
- Synchronized versions across README.md, CLAUDE.md, and plugin.json files
- Asha plugin v1.5.0 with robust memory indexing (retry logic, diagnostics)

### v1.3.0 (2026-01-07)

- Audit and cleanup: Removed stale memory-session-manager references
- Panel system v4.2.0 with --format and --context flags
- Fixed repository structure documentation

### v1.2.0 (2025-11-17)

- Removed AAS-specific universe references
- Updated character names to general-purpose versions:
  - "Asha" → "The Moderator"
  - "The Recruiter" → "The Analyst"
  - "The Adversary" → "The Challenger"
- Generalized character file conventions
- Updated all examples and task patterns with new names

### v1.0.0 (2025-11-17)

- Initial CLAUDE.md creation
- Comprehensive repository analysis
- Documentation of all conventions and patterns
- Plugin system architecture documentation
- Memory system integration guide
- Development workflows and common tasks

---

**Maintained by**: AI assistants working on asha
**Review Cycle**: Update when major structural changes occur
**Validation**: Verify against actual codebase quarterly
