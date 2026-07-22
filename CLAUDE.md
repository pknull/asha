# CLAUDE.md - AI Assistant Guide for asha

**Version**: 2.2.0
**Last Updated**: 2026-07-22
**Repository**: pknull/asha

---

> ### ⚠ Install model
>
> This repo is **not** a Claude plugin marketplace (that flow — `marketplace.json`/`plugin.json` registration — was retired). Primitives install via direct symlinks by **`./install.sh`**; engines live in `lib/`, and the top-level `install.sh`/`uninstall.sh` are thin shims. Launch through the unified **`asha`** dispatcher — `asha <harness>` (claude|codex|copilot), auto-configuring on first use; `asha-claude`/`asha-codex`/`asha-copilot` remain back-compat shims. Authoritative: **[INSTALLER.md](INSTALLER.md)**.

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

**asha** is a multi-harness agent toolkit (Claude Code, Codex, Copilot, OpenCode) providing tools for multi-perspective analysis, code review, creative writing, and session coordination. It installs via direct symlink-mount (`./install.sh`), **not** as a plugin marketplace — see [INSTALLER.md](INSTALLER.md).

### Current Plugins

| Plugin | Version | Domain | Description |
|--------|---------|--------|-------------|
| **Session** | v1.3.0 | Core | Memory persistence, `/save` synthesis, guardrail hooks, autonomous loops |
| **Asha** | v2.1.0 | Identity | Persona templates (`soul.md`, `voice.md`) consumed by `/session:init` |
| **Panel System** | v5.0.0 | Research | Multi-perspective analysis with persistence and resumption — 6 agents |
| **Code** | v1.4.0 | Development | Code review, orchestration patterns, TDD — 5 agents, postgres skill |
| **Write** | v1.6.0 | Creative | Prose craft, continuity, and style analysis — 10 agents, 4 skills |
| **Image** | v2.0.0 | Creative | Stable Diffusion prompts, ComfyUI workflows (skill only) |
| **Admin** | v0.2.0 | Integrations | REST-direct skills: Todoist, Gemini search, Wolfram, BookStack |
| **Security** | v1.0.0 | Security | Web-app security review checklist skill |
| **Test** | — | Tooling | Installer canary (`/test:ping` command/skill/agent) |

### Technology Stack

- **Primary Format**: Markdown (commands, agents, documentation)
- **Scripting**: Bash (hooks, automation), Python (session tools)
- **Configuration**: JSON, YAML frontmatter
- **Platforms**: Claude Code, OpenAI Codex, GitHub Copilot CLI
- **Version Control**: Git

---

## Repository Structure

```
asha/
├── bin/                              # asha dispatcher, drift-check, env bootstrap
├── harnesses/                        # per-harness launch shims (claude.sh, codex.sh, copilot.sh)
├── identity/                         # persona system prompt + identity/operational merge scripts
├── lib/                              # install/uninstall/doctor/build/init-repo engines
├── namespaces.json                   # plugin dir → command namespace map (panel → panel-system)
├── plugins/
│   ├── admin/                        # skills/ (bookstack, gemini, todoist, wolfram)
│   ├── asha/                         # templates/ (soul.md, voice.md) — identity only
│   ├── code/                         # development workflows
│   │   ├── agents/                   # 5 agents (codebase-historian, debugger,
│   │   │                             #   refactor-cleaner, reviewer, tdd)
│   │   ├── commands/                 # review.md, verify.md, orchestrate.md
│   │   ├── skills/postgres/
│   │   ├── hooks/                    # post-edit-lint
│   │   ├── recipes/                  # 5 multi-agent workflows
│   │   ├── modules/                  # code, orchestration, complexity-routing, parallel-agents
│   │   ├── templates/                # harness instruction templates (copilot/cursor/devin)
│   │   └── tools/                    # verify.py
│   ├── image/                        # skills/generation/ (installs as image-generation)
│   ├── panel/                        # research & analysis
│   │   ├── agents/                   # 6 agents (thinker, questioner, examiner,
│   │   │                             #   codifier, recruiter, fabricator)
│   │   ├── commands/panel.md         # /panel command
│   │   ├── docs/characters/          # character profiles
│   │   └── templates/                # seed.yaml
│   ├── security/                     # skills/security-review/
│   ├── session/                      # core scaffold
│   │   ├── commands/                 # init, save, status, silence, restore, loop
│   │   ├── agents/loop-operator.md
│   │   ├── skills/                   # memory-maintenance, skill-creator
│   │   ├── hooks/                    # hooks.json, handlers/, policies/rules.json
│   │   ├── modules/                  # CORE, cognitive, research, memory-ops,
│   │   │                             #   high-stakes, verbalized-sampling
│   │   ├── templates/                # Memory Bank + loop templates
│   │   └── tools/                    # save pipeline, jsonl_reader, learnings, event_store …
│   ├── test/                         # installer canary (ping command/skill/agent, stop hook)
│   └── write/                        # creative writing
│       ├── agents/                   # 10 agents
│       ├── commands/                 # init-novel, review-section
│       ├── skills/                   # book-export, languagetool, novel-state,
│       │                             #   style-analyzer
│       ├── recipes/                  # 3 writing workflows
│       ├── engines/                  # rp-draft-loop.js
│       ├── craft/                    # craft-core-universal, director-rubric
│       └── modules/writing.md
├── docs/                             # harness-enforcement.md, memory-architecture.md, …
├── tests/                            # validation suites + python unit tests
├── templates/                        # init-repo scaffolding
├── install.sh / uninstall.sh         # thin shims over lib/
├── INSTALLER.md
├── .gitignore
├── LICENSE (MIT)
├── README.md
└── CLAUDE.md (this file)
```

### Critical File Paths

| Path | Purpose |
|------|---------|
| `namespaces.json` | Maps plugin directory → slash-command namespace (used by the installer) |
| `lib/install.sh` / `lib/uninstall.sh` | Install/uninstall engines (top-level scripts are thin shims) |
| `harnesses/*.sh` | Per-harness launch wrappers (persona injection) |
| `identity/` | Merged-identity system prompt + merge scripts |
| `plugins/[name]/commands/*.md` | User-facing slash commands |
| `plugins/[name]/agents/*.md` | Agent definitions for deployment |
| `plugins/[name]/skills/*/SKILL.md` | On-demand skills |
| `plugins/[name]/hooks/hooks.json` | Lifecycle hook configuration |
| `docs/harness-enforcement.md` | Single source of truth for cross-harness capability verdicts |

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

- **Command-Based**: Explicit user invocation (`/panel`, `/code:review`, `/session:save`)
- **Hook-Based**: Intervention and context injection (SessionStart, PreToolUse guardrails, PostToolUse lint, UserPromptSubmit, SessionEnd)
- **Skill-Based**: Autonomous guidance (memory-maintenance, postgres, image-generation)
- **Marker-Based**: Control flow via marker files (silence, rp-active)

---

## Plugin System

### Plugin Structure Standard

Every plugin follows this structure:

```
[plugin-name]/
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
├── tools/                    # Optional: Utility scripts
│   └── [script]
├── docs/                     # Optional: Documentation
│   └── [doc].md
├── README.md                 # Required: Plugin overview (carries the **Version** header)
└── LICENSE                   # Required: License file
```

There is no per-plugin metadata file: the installer discovers `commands/`, `agents/`, `skills/`, and `hooks/hooks.json` by convention, and the plugin's version lives in its README's `**Version**:` header. The directory → namespace mapping lives in top-level `namespaces.json`.

---

## Development Workflows

### Adding a New Plugin

1. **Create plugin directory structure**

   ```bash
   mkdir -p plugins/[plugin-name]/{commands,agents,skills,docs}
   ```

2. **Register the namespace**
   - Add a `"dir-name": "namespace"` entry to top-level `namespaces.json` (usually 1:1 with the directory name)

3. **Implement functionality**
   - Commands: Markdown with optional YAML frontmatter
   - Agents: Markdown with agent definition
   - Hooks: Bash scripts + hooks.json registry
   - Skills: SKILL.md in named directory

4. **Write documentation**
   - README.md with usage examples and a `**Version**:` header (this is where the plugin version lives)
   - Add LICENSE file (MIT recommended)

5. **Test installation**

   ```bash
   ./install.sh --only [plugin-name]   # or ./install.sh to (re)install all
   ```

### Modifying Existing Plugins

1. **Read existing implementation**
   - Review the plugin README for structure and version
   - Read command/agent/hook files
   - Check docs/ for specifications

2. **Make changes incrementally**
   - Update the version in the plugin README (increment minor for content, major for structure)
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
mkdir -p "$PROJECT_DIR/Work/markers"
```

### Documentation: single source of truth for harness verdicts

Cross-harness capability and enforcement **verdicts** — what works on Claude, Codex, Copilot, and OpenCode — live in **one** place: [`docs/harness-enforcement.md`](docs/harness-enforcement.md). Every other doc describes mechanism and links to that document for current status.

This is the `feedback_no_duplication` rule applied to prose: the same status fact lived in five docs and drifted three times in a single session. When a capability changes, edit `harness-enforcement.md` and add a README Version History line — do not hand-propagate the claim across satellite docs.

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
| `Work/markers/silence` | Disable all Memory persistence | `/session:silence on` | `/session:silence off` |
| `Work/markers/rp-active` | Enable RP routing and suppress ordinary watching | Manual | session-end hook |
| `Work/markers/prompt-refine` | Enable LanguageTool API | Manual | Manual |

### Hook Behavior with Markers

**All hooks check markers first**:

```bash
# Exit silently if silence marker exists
if [ -f "$PROJECT_DIR/Work/markers/silence" ]; then
    echo "{}" >&2
    exit 0
fi

# Exit silently if RP mode active (session watching only)
if [ -f "$PROJECT_DIR/Work/markers/rp-active" ]; then
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

1. **Plugin Registration**
   - [ ] Plugin directory mapped in `namespaces.json` (new namespaces only)
   - [ ] Version incremented appropriately in the plugin README
   - [ ] All shipped primitives (commands, agents, skills, hooks) exist on disk

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

### Automated Test Suite

Run `./tests/run-tests.sh` for the full suite (plugin validation, version consistency, hook handlers, Python unit tests, optional shellcheck — see README's Testing section for the breakdown). Beyond that, the repo relies on documentation-driven testing:

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

4. **Reinstall to mount it**
   - No registration needed — the installer auto-discovers `commands/*.md`
   - Re-run `./install.sh --only [plugin-name]` (Codex/Copilot regenerate command-skills; symlinks alone don't propagate new commands there)

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

2. **Update the plugin README**

   ```markdown
   **Version**: X.Y.Z
   ```

   (The plugin README is the single home for a plugin's version — there is no plugin.json.)

3. **Update documentation**
   - Top-level README.md version history
   - CLAUDE.md last updated timestamp
   - Any version references in docs

4. **Commit with version tag**

   ```bash
   git commit -m "chore: Bump version to X.Y.Z"
   git tag vX.Y.Z
   ```

### Task: Debug Hook Not Triggering

1. **Check marker files**

   ```bash
   ls -la Work/markers/
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
   - Verify hooks.json (and any other JSON) is valid
   - Check command/agent/skill files exist at expected paths
   - Test end-to-end installation flow (`./install.sh --only [plugin]`, `asha doctor`)

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
   - Panel system: `/panel` command, 6 agents, character profiles, recruitment (Research domain)
   - Code: `/code:review`/`verify`/`orchestrate`, 5 agents, postgres skill (Development domain)
   - Write: 10 writing agents, recipes, prose craft (Creative domain)
   - Session: `/session:*` commands, Memory Bank, core modules, hooks (Core scaffold)
   - Asha: identity templates only (`soul.md`, `voice.md`)
   - Admin / Security / Image: skill-only plugins (integrations, review checklist, image generation)

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

3. **Don't break hooks.json structure**
   - Always validate JSON before committing
   - Test that paths resolve correctly
   - Use `${CLAUDE_PLUGIN_ROOT}` for hook commands

4. **Don't skip version increments**
   - Every content change = minor bump
   - Every structure change = major bump
   - Update the plugin README's `**Version**:` header (+ top-level README history)

5. **Don't ignore marker files**
   - Silence marker = no Memory logging
   - RP-active marker = no session watching
   - Hooks exit silently if markers present

---

## Additional Resources

### Documentation Files

- `README.md`: Toolkit overview and per-plugin summaries
- `INSTALLER.md`: Install model, per-harness layouts
- `docs/harness-enforcement.md`: Cross-harness capability verdicts (single source of truth)
- `docs/memory-architecture.md`: Memory scopes and lifecycle
- `plugins/panel/README.md`: Panel system documentation

### Key Configuration Files

- `namespaces.json`: Plugin directory → command namespace map
- `lib/install.sh` / `lib/uninstall.sh`: Install/uninstall engines
- `plugins/session/hooks/hooks.json`: Session lifecycle hook wiring
- `plugins/session/hooks/policies/rules.json`: PreToolUse policy guardrails
- `~/.asha/config.json`: Cross-project settings (incl. `asha_root` for bare launches)

### External References

- **Claude Code Documentation**: https://docs.claude.com/en/docs/claude-code/
- **Repository Issues**: https://github.com/pknull/asha/issues
- **MIT License**: https://opensource.org/licenses/MIT

---

## Version History

### v2.2.0 (2026-07-22) — Audit remediation

- Full-project audit (Work/audit/2026-07-22--project-audit.md): all ten findings fixed.
- Session v1.3.0 (dead memory-index feature removed; run-python.sh orphan deleted; jsonl_reader self-contained; skills documented), Code v1.4.0 (`asha calibration` dispatcher verb; postgres skill documented), Admin v0.2.0 (prose de-localized).
- Installer: per-harness failure isolation in `asha_install_main` (mirrors uninstall's issue-#4 pattern).
- `~/life` paths swept from all shipped prose; version tables re-synced.
- Tests: shellcheck scope extended to bin/lib/harnesses/identity; validate-versions cross-checks plugin READMEs vs both top-level tables; new install round-trip + identity-merge smoke suites; bash-safety flags classified repo-wide.

### v2.1.0 (2026-07-10) — Ecosystem audit prune

- **13 → 9 plugin namespaces** — schedule (scheduler), devops, prompt, output-styles retired.
- **Agents 46 → ~23** — write 17→10 (consolidations: continuity-reviewer, prose-analysis, voice-analyst, intimacy-arbiter); code 15→5; database-reviewer → code `postgres` skill; image-engineer → image `generation` skill; book-maker absorbed into book-export.
- **Commands 23 → 14, skills 24 → 15** — `/asha:init` merged into `/session:init`; session spawn/agents/stop-agents/note/prime, code:checkpoint, partner-sentiment, task-manager, verify-app removed (verify lives on as `/code:verify`).
- **Portable-first policy adopted** — Claude-native equivalents are never sufficient removal grounds for cross-harness components.
- **Panel**: all 6 agents gained frontmatter (delegable on Claude); vendored `fabricator` replaces the external agent-fabricator dependency; harness-aware Role Execution Model in `/panel`.
- **ASHA_ROOT config fallback** — resolves from `~/.asha/config.json` under bare launches.
- Doc sync: marketplace-era sections (plugin.json / marketplace.json) removed from this guide. Full rulings: `Work/panels/2026-07-10--ecosystem-audit/`.

### v2.0.0 (2026-06-18) — Asha learnings: OKF bundle

- **Breaking (on-disk format):** learnings moved from a single flat `~/.asha/learnings.md` to an OKF concept bundle (`~/.asha/learnings/`, one file per learning, `type: learning`, auto-generated `index.md`). One-way migration via `plugins/session/tools/migrate_learnings_to_okf.py`; older asha versions cannot read the bundle — pin the matching version per repo.
- Upsert-by-id dedup; vendored OKF `validate.py`/`visualize.py`; warn-only validate-on-`/save`.
- Auto-suggested `## Related` cross-links at interactive `/save` (semantic, non-blocking).
- New `docs/memory-architecture.md` (scopes, lifecycle, "is it providing value?" guide).

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
