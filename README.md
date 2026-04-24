# asha-marketplace

**Version**: 1.17.0
**Description**: Claude Code plugins for research, development, creative writing, image generation, scheduling, and session coordination

A collection of domain-focused Claude Code plugins organized by workflow type.

---

## Install model: symlink-mount (no marketplace registry)

Instead of the legacy three-file plugin registration chain
(`marketplace.json` → `installed_plugins.json` → `enabledPlugins`),
installation is now a plain symlink operation:

```bash
./install.sh              # symlinks primitives into ~/.claude/*
./uninstall.sh            # removes marketplace-sourced symlinks
./deprecate-marketplace.sh  # one-shot cleanup of legacy registration state
```

See **[INSTALLER.md](INSTALLER.md)** for the model and `plugins/test/README.md`
for the smoke-test plugin.

The tables below still describe what each plugin does; the "install" mechanism
is only the one above.

---

## Plugin Domains

| Domain | Plugin | Version | Purpose |
|--------|--------|---------|---------|
| **Research** | `panel-system` | v5.0.0 | Multi-perspective analysis, expert panels, decision-making |
| **Development** | `code` | v1.11.0 | Code review, orchestration patterns, TDD, 15 agents |
| **Creative** | `write` | v1.5.0 | Fiction writing, prose craft, perplexity detection, 16 agents |
| **Image** | `image` | v1.1.0 | Stable Diffusion prompts, ComfyUI workflows |
| **Automation** | `scheduler` | v0.1.0 | Cron-style scheduled task execution |
| **Formatting** | `output-styles` | v1.0.2 | Response styling and output formats |
| **Core** | `asha` | v1.18.0 | Session coordination, memory persistence, learnings |

### When to Use Each

**panel-system** — When you need multiple perspectives on a question

- Architecture decisions, trade-off analysis
- Creative brainstorming with diverse viewpoints
- Risk assessment, devil's advocacy

**code** — When you're building software

- Code review before commits
- Multi-agent feature implementation
- Bug investigation, refactoring, TDD

**write** — When you're writing fiction

- Chapter drafting with perplexity validation
- Style analysis from exemplar texts
- Manuscript revision workflows

**image** — When you need AI-generated images

- Stable Diffusion prompt engineering
- ComfyUI workflow design
- LoRA/model selection guidance

**scheduler** — When you need automated recurring tasks

- Daily code reviews
- Scheduled reports
- Automated maintenance

**asha** — Always (foundation)

- Session memory across conversations
- Cross-project identity via `~/.asha/`
- Confidence-tracked learnings that persist

---

## Available Plugins

### Panel System

**Plugin Name**: `panel-system`
**Command**: `/panel`
**Version**: 5.0.0
**Domain**: Research & Analysis

Dynamic multi-perspective analysis with 3 core roles (Moderator, Analyst, Challenger) + dynamically recruited specialists. Full state persistence for resumption and audit.

```bash
/panel Should we implement GraphQL or REST for the new API
/panel --format=github "Review authentication approach"
/panel --context=docs/RFC.md "Evaluate this proposal"

# Panel management
/panel --list                    # List all panels
/panel --list --status=active    # Filter by status
/panel --resume <id>             # Resume interrupted panel
/panel --show <id>               # Display panel summary
/panel --abandon <id>            # Mark as abandoned
```

**Features**:

- 11-phase structured decision protocol
- Consensus tracking with percentage thresholds
- Output formats: markdown (default), github, json
- Context injection from files or URLs
- Dynamic specialist recruitment
- Full persistence with `--resume`, `--list`, `--show`, `--abandon`
- Per-phase state files in `Work/panels/` for audit trail

**[Full Documentation →](plugins/panel/README.md)**

---

### Code

**Plugin Name**: `code`
**Commands**: `/code:review`, `/code:verify`, `/code:checkpoint`, `/code:orchestrate`
**Version**: 1.11.0
**Domain**: Development

Development workflows with orchestration patterns, code review, TDD, and 15 specialized agents.

```bash
/code:review              # Review staged changes
/code:review <path>       # Review specific file(s)
/code:review --all        # Review all uncommitted changes
/code:verify              # Run types, lint, tests, security
/code:checkpoint "name"   # Create named progress checkpoint
```

**Agents** (15):

| Agent | Role |
|-------|------|
| **architect** | System architecture and modular design |
| **build-error-resolver** | Build and TypeScript error resolution |
| **code-reviewer** | Code quality and security review |
| **codebase-historian** | Prior art discovery, pattern archaeology |
| **database-reviewer** | PostgreSQL optimization, RLS policies |
| **debugger** | Complex issue diagnosis, root cause analysis |
| **doc-updater** | Documentation sync from code structure |
| **e2e-runner** | Playwright E2E testing |
| **go-build-resolver** | Go compilation error specialist |
| **go-reviewer** | Idiomatic Go review |
| **javascript-pro** | Modern ES2023+ development |
| **python-pro** | Python 3.11+ type-safe development |
| **refactor-cleaner** | Dead code removal, cleanup |
| **tdd** | Test-driven development (London School) |
| **typescript-pro** | Advanced TypeScript development |

**Skills**: Django patterns, Spring Boot patterns, Go patterns, Python patterns, API design

**Recipes** (multi-agent workflows):

| Recipe | Use Case |
|--------|----------|
| `feature-implementation.yaml` | New features end-to-end |
| `bug-investigation.yaml` | Bug diagnosis and fix |
| `refactor-safe.yaml` | Code cleanup with safety |
| `security-audit.yaml` | Security hardening |

**[Full Documentation →](plugins/code/README.md)**

---

### Write

**Plugin Name**: `write`
**Commands**: `/write:perplexity`, `/write:init-novel`, `/write:review-section`
**Version**: 1.5.0
**Domain**: Creative Writing

Creative writing workflows with prose craft, perplexity detection, style analysis, and 16 specialized agents.

```bash
/write:perplexity chapter.md     # Check prose for AI flatness
/write:init-novel /path/to/proj  # Initialize novel state structure
/write:review-section            # Run periodic review suite
```

**Agents** (16):

| Agent | Role |
|-------|------|
| **outline-architect** | Story structure, beat sheets, chapter outlines |
| **prose-writer** | Draft generation with voice anchoring |
| **fiction-writer** | Primary creative coordinator for full pipeline |
| **consistency-checker** | Continuity tracking (characters, timelines, lore) |
| **developmental-editor** | Arc analysis, pacing, structural review |
| **line-editor** | Sentence craft, word choice, polish |
| **prose-analysis** | Multi-mode prose review (voice, continuity, coherence) |
| **intimacy-designer** | Adult content specialist (scene frameworks, boundaries) |
| **manuscript-editor** | Structural editing and revision coordination |
| **novel-character-reviewer** | Character consistency validation |
| **novel-continuity-reviewer** | Timeline, spatial logic, knowledge boundaries |
| **novel-state-updater** | State extraction after validation |
| **novel-style-linter** | Voice compliance, variance metrics |
| **book-analyzer** | Extract quantified style rules from exemplar texts |
| **bible-merger** | Consolidate multiple analyses into unified voice.md |
| **perplexity-improver** | Rewrite flat prose using VS-Tail sampling |

**Skills**:

| Skill | Purpose |
|-------|---------|
| **perplexity-gate** | Local prose flatness detection (Ollama + Ministral) |
| **style-analyzer** | Quantified prose analysis (sentence metrics, dialogue, vocabulary) |
| **novel-state** | Directory structure for manuscript state tracking |
| **languagetool** | Grammar and style checking via local server |
| **book-export** | Professional PDF/ePub export with styling profiles |
| **book-maker** | Python-based markdown converter |

**Recipes** (multi-agent workflows):

| Recipe | Use Case |
|--------|----------|
| `chapter-creation.yaml` | New chapter with perplexity gate |
| `manuscript-revision.yaml` | Complete revision of existing draft |
| `character-development.yaml` | Deep character creation with voice testing |

**[Full Documentation →](plugins/write/README.md)**

---

### Image

**Plugin Name**: `image`
**Version**: 1.1.0
**Domain**: AI Image Generation

Stable Diffusion prompt engineering and ComfyUI workflow design.

```bash
/plugin install image@asha-marketplace
```

**Agent**: `comfyui-prompt-engineer`

- Image generation prompts from concept descriptions
- ComfyUI workflow JSON construction
- LoRA/model selection guidance
- Prompt iteration based on output feedback

**Usage**:

```
Design a prompt for: ethereal forest scene with bioluminescent mushrooms
Create a ComfyUI workflow for: txt2img with upscaling
```

**[Full Documentation →](plugins/image/README.md)**

---

### Scheduler

**Plugin Name**: `scheduler`
**Command**: `/schedule`
**Version**: 0.1.0
**Domain**: Automation

Cron-style scheduled task execution with natural language time expressions.

```bash
/schedule "Every weekday at 9am" "Review code changes since yesterday"
/schedule list                    # Show all tasks
/schedule show <id>               # Task details
/schedule remove <id>             # Delete task
/schedule logs <id>               # View execution output
```

**Time Expressions**:

| Expression | Cron Equivalent |
|------------|-----------------|
| "Every day at 9am" | `0 9 * * *` |
| "Every weekday at 9am" | `0 9 * * 1-5` |
| "Every Monday at 2pm" | `0 14 * * 1` |
| "Every hour" | `0 * * * *` |
| "Every 15 minutes" | `*/15 * * * *` |

**Security**:

- Default read-only mode (Read, Grep, Glob only)
- Max 10 tasks per project
- Dangerous command patterns blocked
- Audit logging for all operations

**[Full Documentation →](plugins/schedule/README.md)**

---

### Output Styles

**Plugin Name**: `output-styles`
**Command**: `/style`
**Version**: 1.0.2
**Domain**: Formatting

Switchable output styles for Claude Code responses.

```bash
/style                    # List available styles
/style <name>             # Switch to a style
/style off                # Disable styling
```

**Available Styles**:

| Style | Description |
|-------|-------------|
| ultra-concise | Minimal words, direct actions |
| bullet-points | Hierarchical bullet points |
| genui | Generative UI with HTML output |
| html-structured | Clean semantic HTML |
| markdown-focused | Full markdown features |
| table-based | Table-based organization |
| tts-summary | Audio TTS announcements |
| yaml-structured | YAML structured output |

---

### Asha

**Plugin Name**: `asha`
**Commands**: `/asha:init`, `/asha:save`, `/asha:prime`, `/asha:note`, `/asha:status`, `/asha:loop`, `/asha:spawn`, `/asha:agents`, `/asha:silence`, `/asha:restore`
**Version**: 1.18.0
**Domain**: Core Scaffold

Cognitive scaffold framework with cross-project identity, automatic learning, and session coordination. Foundation layer that other plugins build on.

```bash
/asha:init                # Initialize (creates ~/.asha/ + project Memory/)
/asha:save                # Synthesize session + extract learnings
/asha:prime               # Interactive codebase exploration
/asha:note "text"         # Add timestamped note
/asha:status              # Show session status
/asha:loop                # Start autonomous agent loop
/asha:spawn <agent>       # Spawn agent in tmux
/asha:silence             # Disable Memory logging
```

**Two-Layer Architecture**:

| Layer | Location | Purpose |
|-------|----------|---------|
| **Identity** | `~/.asha/` | Cross-project (who Asha is, who you are) |
| **Project** | `Memory/` | Per-project state, protocols, tech stack |

**Identity Layer** (`~/.asha/` — user-scope, persists across all projects):

| File | Purpose |
|------|---------|
| `soul.md` | Who Asha is (identity, values, nature) |
| `voice.md` | How Asha expresses (tone, patterns) |
| `keeper.md` | Who you are (preferences, calibration signals) |
| `learnings.md` | Patterns with confidence tracking (0.3-0.9) |
| `config.json` | Cross-project settings |

**Project Layer** (`Memory/` — git-committed):

| File | Purpose |
|------|---------|
| `activeContext.md` | Current session state |
| `projectbrief.md` | Project foundation |
| `techEnvironment.md` | Tools and platform config |
| `workflowProtocols.md` | Project-specific patterns |

**Agents** (4):

| Agent | Role |
|-------|------|
| **partner-sentiment** | Haiku generation for session continuity |
| **task-manager** | Todoist integration for task retrieval |
| **verify-app** | Post-change verification (tests, types, lint) |
| **loop-operator** | Autonomous workflow with safety guardrails |

**Core Modules** (general techniques):

| Module | Purpose |
|--------|---------|
| `CORE.md` | Bootstrap protocol, identity, memory architecture |
| `cognitive.md` | ACE cycle, parallel execution, tool efficiency |
| `research.md` | Authority verification, citation standards |
| `memory-ops.md` | Session synthesis, Memory Bank maintenance |
| `high-stakes.md` | Safety protocols for destructive operations |
| `verbalized-sampling.md` | Mode collapse recovery, diversity generation |

---

## Installation

### Add Marketplace

```bash
/plugin marketplace add pknull/asha-marketplace
```

### Install Plugins

```bash
# Foundation (recommended first)
/plugin install asha@asha-marketplace

# Domain plugins (install as needed)
/plugin install panel-system@asha-marketplace
/plugin install code@asha-marketplace
/plugin install write@asha-marketplace
/plugin install image@asha-marketplace
/plugin install scheduler@asha-marketplace
/plugin install output-styles@asha-marketplace
```

### Verify Installation

```bash
/plugin list
```

---

## Plugin Directory Structure

```
asha-marketplace/
├── .claude-plugin/
│   └── marketplace.json          # Marketplace metadata
├── plugins/
│   ├── panel/                    # Research & analysis
│   │   ├── .claude-plugin/
│   │   ├── commands/
│   │   ├── agents/
│   │   └── docs/characters/
│   ├── code/                     # Development workflows
│   │   ├── .claude-plugin/
│   │   ├── commands/
│   │   ├── agents/ (15)
│   │   ├── skills/
│   │   └── recipes/
│   ├── write/                    # Creative writing
│   │   ├── .claude-plugin/
│   │   ├── commands/
│   │   ├── agents/ (16)
│   │   ├── skills/
│   │   └── recipes/
│   ├── image/                    # AI image generation
│   │   ├── .claude-plugin/
│   │   └── agents/
│   ├── schedule/                 # Task scheduling
│   │   ├── .claude-plugin/
│   │   ├── commands/
│   │   ├── agents/
│   │   ├── hooks/
│   │   └── tools/
│   ├── output-styles/            # Response formatting
│   │   ├── .claude-plugin/
│   │   ├── commands/
│   │   ├── hooks/
│   │   └── styles/
│   └── asha/                     # Core scaffold
│       ├── .claude-plugin/
│       ├── commands/
│       ├── hooks/
│       ├── modules/
│       ├── skills/
│       ├── templates/
│       └── tools/
├── README.md
├── CLAUDE.md
└── LICENSE
```

---

## Testing

Run the full test suite:

```bash
./tests/run-tests.sh
```

### Test Coverage

| Suite | Tests | Description |
|-------|-------|-------------|
| Plugin Validation | 5 | JSON schema, namespace conflicts, file existence |
| Version Consistency | 6 | Cross-file version synchronization |
| Python Unit Tests | 67 | reasoning_bank, memory_index, local_react_save |
| Hook Handlers | 104 | Lifecycle hooks, rules, tools, repo hygiene |
| Shell Linting | 1 | shellcheck (optional) |

**Total: 182 tests** (183 with shellcheck)

Individual test suites:

```bash
./tests/validate-plugins.sh    # Plugin configuration
./tests/validate-versions.sh   # Version consistency
./tests/test-hooks.sh          # Hook handlers
python3 -m unittest discover -s tests/python -v  # Python tests
```

---

## Contributing

To propose new plugins or improvements:

1. Fork this repository
2. Create plugin in new subdirectory following structure
3. Update `.claude-plugin/marketplace.json`
4. Run `./tests/run-tests.sh` to verify all tests pass
5. Submit pull request with documentation

---

## License

Individual plugins licensed separately. See each plugin's LICENSE file.

- **Panel System**: MIT License
- **Code**: MIT License
- **Write**: MIT License
- **Image**: MIT License
- **Scheduler**: MIT License
- **Output Styles**: MIT License
- **Asha**: MIT License

---

## Support

**Issues and feature requests**: https://github.com/pknull/asha-marketplace/issues

**Documentation**:

- Panel system: `plugins/panel/README.md`
- Code workflows: `plugins/code/README.md`
- Writing workflows: `plugins/write/README.md`
- Image generation: `plugins/image/README.md`
- Scheduling: `plugins/schedule/README.md`
- Development guide: `CLAUDE.md`

---

## Version History

### v1.17.0 (2026-03-09)

- **Write v1.5.0**: Claude Book feature parity
  - 3 new agents: book-analyzer, bible-merger, perplexity-improver
  - style-analyzer skill (quantified prose analysis)
  - Total: 16 agents

### v1.16.0 (2026-03-08)

- **Write v1.4.0**: Novel-specific agents from AAS project
  - novel-character-reviewer, novel-continuity-reviewer
  - novel-state-updater, novel-style-linter

### v1.15.0 (2026-03-08)

- **Write v1.3.0**: Perplexity detection and novel state
  - perplexity-gate skill (local Ollama + Ministral)
  - novel-state skill (bible/state/timeline structure)
  - Removed ai-detector (replaced by local perplexity)

### v1.11.0 (2026-02-13)

- **Asha v1.18.0**: Confidence-tracked learnings
  - Learnings rise on confirmation, decay on contradiction
  - Secret scrubbing for event logs
  - ECC review integration

### v1.9.0 (2026-01-29)

- **Panel system v5.0.0**: Full persistence and panel management
  - `--resume <id>`: Continue interrupted panels
  - `--list [--status=X]`: Query panel index
  - Per-phase state files in `Work/panels/`
- **Asha v1.8.0**: Cross-project identity layer
  - `~/.asha/` for identity (soul.md, voice.md, keeper.md)
  - `/asha:save` captures keeper calibration

### v1.8.0 (2026-01-28)

- **Scheduler v0.1.0**: Cron-style task automation
  - Natural language time parsing
  - cron and systemd backend support
  - Rate limiting and security constraints

### v1.7.0 (2026-01-26)

- **Image v1.1.0**: AI image generation
  - comfyui-prompt-engineer agent
  - SD prompt crafting and workflow design

### v1.6.0 (2026-01-26)

- **Domain restructuring**: Organized by workflow type
- **Code v1.1.0**: Development workflows, 15 agents
- **Write v1.2.0**: Creative writing, prose craft

### v1.5.0 (2026-01-16)

- Fixed hook handler permissions
- Version validation script
- Asha v1.5.0 with robust memory indexing

### v1.3.0 (2026-01-07)

- Panel system v4.2.0 with --format and --context flags
- Audit and cleanup of stale references

### v1.0.0 (2025-11-08)

- Initial marketplace release
