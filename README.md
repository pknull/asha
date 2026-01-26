# asha-marketplace

**Version**: 1.6.0
**Description**: Claude Code plugins for research, development, creative writing, and session coordination

A collection of domain-focused Claude Code plugins organized by workflow type.

---

## Plugin Domains

| Domain | Plugin | Purpose |
|--------|--------|---------|
| **Research** | `panel` | Multi-perspective analysis, expert panels, decision-making |
| **Development** | `code` | Code review, orchestration patterns, TDD workflows |
| **Creative** | `write` | Fiction writing, prose craft, worldbuilding |
| **Formatting** | `output-styles` | Response styling and output formats |
| **Core** | `asha` | Session coordination, memory persistence, general techniques |

### When to Use Each

**panel** — When you need multiple perspectives on a question
- Architecture decisions, trade-off analysis
- Creative brainstorming with diverse viewpoints
- Risk assessment, devil's advocacy

**code** — When you're building software
- Code review before commits
- Multi-agent feature implementation
- Bug investigation, refactoring

**write** — When you're writing fiction
- Chapter drafting with staged review
- Character development with voice testing
- Manuscript revision workflows

**asha** — Always (foundation)
- Session memory across conversations
- Identity persistence via Memory Bank
- General techniques (research verification, high-stakes safety, diversity sampling)

---

## Available Plugins

### Panel System

**Plugin Name**: `panel-system`
**Command**: `/panel`
**Version**: 4.2.0
**Domain**: Research & Analysis

Dynamic multi-perspective analysis with 3 core roles (Moderator, Analyst, Challenger) + dynamically recruited specialists.

```bash
/panel Should we implement GraphQL or REST for the new API
/panel --format=github "Review authentication approach"
/panel --context=docs/RFC.md "Evaluate this proposal"
```

**Features**:
- 11-phase structured decision protocol
- Consensus tracking with percentage thresholds
- Output formats: markdown (default), github, json
- Context injection from files or URLs
- Dynamic specialist recruitment

**[Full Documentation →](plugins/panel/README.md)**

---

### Code

**Plugin Name**: `code`
**Command**: `/code:review`
**Version**: 1.0.0
**Domain**: Development

Development workflows with orchestration patterns, code review, and specialized agents.

```bash
/code:review              # Review staged changes
/code:review <path>       # Review specific file(s)
/code:review --all        # Review all uncommitted changes
```

**Agents**:
- **codebase-historian** — Prior art discovery, pattern archaeology

**Modules**:
- **code.md** — ACE cognitive cycle, convention matching, high-stakes protocols
- **orchestration.md** — Quality gates, Socratic planning, scale-adaptive workflows

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
**Version**: 1.0.0
**Domain**: Creative Writing

Creative writing workflows for fiction development: prose craft, worldbuilding, editing, and storytelling agents.

```bash
/plugin install write@asha-marketplace
```

**Agents**:
| Agent | Role |
|-------|------|
| **outline-architect** | Story structure, beat sheets, chapter outlines |
| **prose-writer** | Draft generation with voice anchoring |
| **consistency-checker** | Continuity tracking (characters, timelines, lore) |
| **developmental-editor** | Arc analysis, pacing, structural review |
| **line-editor** | Sentence craft, word choice, polish |

**Modules**:
- **writing.md** — Prose craft guidelines, sentence rhythm, staged draft protocol

**Recipes** (multi-agent workflows):
| Recipe | Use Case |
|--------|----------|
| `chapter-creation.yaml` | New chapter from concept to polish |
| `manuscript-revision.yaml` | Complete revision of existing draft |
| `character-development.yaml` | Deep character creation with voice testing |

**[Full Documentation →](plugins/write/README.md)**

---

### Output Styles

**Plugin Name**: `output-styles`
**Command**: `/style`
**Version**: 1.0.1
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
**Commands**: `/asha:init`, `/asha:save`, `/asha:note`, `/asha:status`, `/asha:index`, `/asha:cleanup`
**Version**: 1.6.0
**Domain**: Core Scaffold

Cognitive scaffold framework for session coordination and memory persistence. Foundation layer that other plugins build on.

```bash
/asha:init                # Initialize Asha in project
/asha:save                # Save session to Memory Bank
/asha:note "text"         # Add timestamped note to scratchpad
/asha:status              # Show session status
/asha:index               # Index files for semantic search
/asha:cleanup             # Remove legacy installation files
```

**Core Modules** (general techniques):
| Module | Purpose |
|--------|---------|
| `CORE.md` | Bootstrap protocol, identity, memory architecture |
| `cognitive.md` | ACE cycle, parallel execution, tool efficiency |
| `research.md` | Authority verification, citation standards, epistemic hygiene |
| `memory-ops.md` | Session synthesis, Memory Bank maintenance |
| `high-stakes.md` | Safety protocols for destructive operations |
| `verbalized-sampling.md` | Mode collapse recovery, diversity generation |

**Memory Bank Templates**:
- `activeContext.md` — Current session state
- `projectbrief.md` — Project foundation
- `communicationStyle.md` — Identity and voice
- `techEnvironment.md` — Tools and platform config
- `workflowProtocols.md` — Project-specific patterns

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
│   │   ├── agents/
│   │   ├── modules/
│   │   └── recipes/
│   ├── write/                    # Creative writing
│   │   ├── .claude-plugin/
│   │   ├── agents/
│   │   ├── modules/
│   │   └── recipes/
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
- **Output Styles**: MIT License
- **Asha**: MIT License

---

## Support

**Issues and feature requests**: https://github.com/pknull/asha-marketplace/issues

**Documentation**:
- Panel system: `plugins/panel/README.md`
- Code workflows: `plugins/code/README.md`
- Writing workflows: `plugins/write/README.md`
- Development guide: `CLAUDE.md`

---

## Version History

### v1.6.0 (2026-01-26)
- **Domain restructuring**: Organized plugins by workflow type
- **New plugin: code** — Development workflows, orchestration patterns, codebase-historian agent
- **New plugin: write** — Creative writing with 5 specialized agents and recipes
- **Absorbed local-review** into code plugin as `/code:review`
- Cleaned up asha to core scaffold only (moved domain content to code/write)

### v1.5.0 (2026-01-16)
- Fixed hook handler permissions and naming consistency
- Added version validation script
- Asha plugin v1.5.0 with robust memory indexing

### v1.3.0 (2026-01-07)
- Audit & cleanup: Removed stale memory-session-manager references
- Panel system v4.2.0 with --format and --context flags

### v1.2.0 (2025-11-17)
- Sterilization: Removed universe-specific references
- Renamed character files to universal archetypes

### v1.1.0 (2025-11-08)
- Panel system v4.1.0 with dynamic recruitment architecture

### v1.0.0 (2025-11-08)
- Initial marketplace release
