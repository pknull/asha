# asha-marketplace

**Version**: 1.3.0
**Description**: Claude Code plugins for multi-perspective analysis, code review, output styling, and session coordination

A collection of general-purpose Claude Code plugins for technical teams, creative projects, and systematic decision-making workflows.

---

## Available Plugins

### Panel System

**Plugin Name**: `panel-system`
**Command**: `/panel`
**Version**: 4.2.0

Dynamic multi-perspective analysis with 3 core roles (Moderator, Analyst, Challenger) + dynamically recruited specialists. Supports multiple output formats and context injection.

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
- Dynamic specialist recruitment from agent library

**[Full Documentation →](plugins/panel/README.md)**

---

### Local Review

**Plugin Name**: `local-review`
**Command**: `/local-review`
**Version**: 1.0.1

Parallel code review with 4 specialized reviewers plus validation pass to filter false positives.

```bash
/local-review              # Review staged changes
/local-review <path>       # Review specific file(s)
/local-review --all        # Review all uncommitted changes
```

**Reviewers**:
- **Security**: Injection, auth flaws, hardcoded secrets
- **Logic**: Algorithms, conditionals, state management
- **Edge Cases**: Null handling, boundaries, race conditions
- **Style**: Naming, duplication, complexity

---

### Output Styles

**Plugin Name**: `output-styles`
**Command**: `/style`
**Version**: 1.0.1

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
**Commands**: `/asha:init`, `/asha:save`, `/asha:index`, `/asha:cleanup`
**Version**: 1.0.1

Cognitive scaffold framework for session coordination and memory persistence.

```bash
/asha:init                # Initialize Asha in project
/asha:save                # Save session to Memory Bank
/asha:index               # Index files for semantic search
/asha:cleanup             # Remove legacy installation files
```

**Features**:
- Memory Bank architecture (activeContext, projectbrief, etc.)
- Session watching and archival
- Vector DB semantic search (optional)
- Git integration for persistence

---

## Installation

### Add Marketplace

```bash
/plugin marketplace add pknull/asha-marketplace
```

### Install Plugins

```bash
/plugin install panel-system@asha-marketplace
/plugin install local-review@asha-marketplace
/plugin install output-styles@asha-marketplace
/plugin install asha@asha-marketplace
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
│   ├── panel/                    # Panel system plugin
│   │   ├── .claude-plugin/
│   │   │   └── plugin.json
│   │   ├── commands/
│   │   │   └── panel.md
│   │   ├── agents/
│   │   │   └── recruiter.md
│   │   ├── docs/characters/
│   │   └── README.md
│   ├── local-review/             # Code review plugin
│   │   ├── .claude-plugin/
│   │   │   └── plugin.json
│   │   └── commands/
│   │       └── local-review.md
│   ├── output-styles/            # Output styling plugin
│   │   ├── .claude-plugin/
│   │   │   └── plugin.json
│   │   ├── commands/
│   │   │   └── style.md
│   │   ├── hooks/
│   │   └── styles/
│   └── asha/                     # Session coordination plugin
│       ├── .claude-plugin/
│       │   └── plugin.json
│       ├── commands/
│       ├── hooks/
│       ├── templates/
│       └── tools/
├── README.md
├── CLAUDE.md
└── LICENSE
```

---

## Contributing

To propose new plugins or improvements:

1. Fork this repository
2. Create plugin in new subdirectory following structure
3. Update `.claude-plugin/marketplace.json`
4. Submit pull request with documentation

---

## License

Individual plugins licensed separately. See each plugin's LICENSE file.

- **Panel System**: MIT License
- **Local Review**: MIT License
- **Output Styles**: MIT License
- **Asha**: MIT License

---

## Support

**Issues and feature requests**: https://github.com/pknull/asha-marketplace/issues

**Documentation**:
- Panel system: `plugins/panel/README.md`
- Development guide: `CLAUDE.md`

---

## Version History

### v1.3.0 (2026-01-07)
- **Audit & cleanup**: Removed stale memory-session-manager references
- Panel system v4.2.0 with --format and --context flags
- Added local-review, output-styles, and asha plugins to documentation
- Fixed merge conflicts in README

### v1.2.0 (2025-11-17)
- Sterilization: Removed universe-specific references
- Renamed character files to universal archetypes

### v1.1.0 (2025-11-08)
- Panel system v4.1.0 with dynamic recruitment architecture
- 3 core roles + recruited specialists

### v1.0.0 (2025-11-08)
- Initial marketplace release
