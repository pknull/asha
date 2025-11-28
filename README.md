# Asha

Portable session coordinator framework for AI assistants.

## What This Is

Asha provides a structured framework for AI session coordination:

- **CORE.md** - The WIREFRAME: operational protocols, authority verification, cognitive cycles, output standards
- **templates/** - Memory file starters for bootstrapping new projects
- **docs/** - Supporting documentation for Memory architecture and session management

The framework is voice-agnostic. CORE.md defines *how* to operate. Each project's `Memory/communicationStyle.md` defines *who* to be.

## Directory Structure

```
Asha/
├── CORE.md                 # Framework (portable, voice-agnostic)
├── README.md               # This file
├── docs/
│   ├── MEMORY-STRUCTURE.md # Memory Bank architecture
│   ├── SESSION-CAPTURE.md  # Session watching protocol
│   └── SESSION-SAVE.md     # Session synthesis protocol
└── templates/
    ├── activeContext.md    # Session state template
    ├── projectbrief.md     # Project foundation template
    ├── communicationStyle.md # Voice/persona template
    ├── techEnvironment.md  # Technical context template
    └── workflowProtocols.md # Execution patterns template
```

## Deployment

### 1. Copy Asha to Your Project

```bash
cp -r /path/to/Asha /your/project/
```

### 2. Create Entry Point

Create a file that references the core framework. The filename depends on your AI platform:

| Platform | File |
|----------|------|
| Claude Code | `CLAUDE.md` |
| Gemini CLI | `GEMINI.md` |
| Codex CLI | `AGENTS.md` |
| Other | Check platform documentation |

Entry point content:
```markdown
@Asha/CORE.md
```

### 3. Initialize Memory

Create a `Memory/` directory and copy templates:

```bash
mkdir Memory
cp Asha/templates/*.md Memory/
```

### 4. Configure Memory Files

Edit each Memory file to replace placeholder content:

1. **projectbrief.md** - Define your project scope, objectives, constraints
2. **communicationStyle.md** - Define voice, persona, authority hierarchy
3. **techEnvironment.md** - Define tools, paths, platform capabilities
4. **workflowProtocols.md** - Define project-specific execution patterns
5. **activeContext.md** - Initialize with current project state

### 5. Optional: Session Infrastructure

For full session capture and synthesis:

1. Create `Work/` directory for ephemeral session data
2. Create `Memory/sessions/` for session archives
3. Configure hooks per `docs/SESSION-CAPTURE.md`
4. Configure save command per `docs/SESSION-SAVE.md`

## Minimal Deployment

For quick starts without full infrastructure:

1. Copy `Asha/` to project
2. Create entry point referencing `@Asha/CORE.md`
3. Create `Memory/` with at least:
   - `activeContext.md` (what's happening now)
   - `projectbrief.md` (what this project is)

The framework operates in degraded mode without full Memory, prompting for initialization when files are missing.

## Framework Components

### WIREFRAME Structure (CORE.md)

| Section | Purpose |
|---------|---------|
| W - Who & What | Identity, competencies |
| I - Input | Memory access protocol, context handling |
| R - Rules | Authority verification, constraints, error handling |
| E - Expected Output | Format standards, examples |
| F - Flow | Task execution workflow, ACE cycle |
| R - Reference | Core file inventory |
| A - Ask | Clarification protocols |
| M - Memory | Maintenance triggers, frontmatter schema |
| E - Evaluate | Execution protocol |

### Key Protocols

- **Authority Verification**: [Inference], [Speculation], [Unverified] markers
- **ACE Cognitive Cycle**: Generate approaches → Reflect on blind spots → Curate recommendation
- **Four Questions Protocol**: Goal → Accomplishments → Learnings → Next steps

## Customization

### Voice/Persona

Edit `Memory/communicationStyle.md` to define:
- Primary identity and form
- Voice constraints (prohibited/required patterns)
- Tone calibration per project context
- Authority hierarchy

### Project-Specific Protocols

Edit `Memory/workflowProtocols.md` to add:
- Domain-specific validation rules
- Verified patterns from project experience
- Anti-patterns to avoid

### Technical Environment

Edit `Memory/techEnvironment.md` to define:
- System environment details
- Tool stack and integrations
- Directory structure
- Code conventions

## Memory Management

Updating and maintaining Memory is a per-project, per-tooling exercise. The framework defines *what* Memory should contain and *when* to update it. The *how* depends on your project's infrastructure.

Example approaches:

- **Manual**: Edit Memory files directly, commit changes with git
- **File-based with search**: Store Memory as markdown, use a vector database for semantic search across files (the AAS project uses ChromaDB + Ollama embeddings)
- **Structured storage**: Database-backed Memory with query interfaces
- **Hybrid**: Combination of approaches based on content type

The templates provide structure. The tooling is yours to define in `Memory/techEnvironment.md`.

## Version

This framework version extracted from AAS project, 2025-11-27.
