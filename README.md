# Asha Marketplace

**Version**: 1.2.0
**Description**: Claude Code plugins and tools for the Asha framework

Collection of plugins supporting creative writing, TTRPG campaigns, narrative development, and the Redacted107 (REDACTED99) universe.

---

## Available Plugins

### Panel System

**Plugin Name**: `panel-system`
**Command**: `/panel`
**Version**: 4.1.2
**Description**: Dynamic multi-perspective analysis with automatic specialist recruitment

**[Full Documentation →](plugins/panel/README.md)**

---

## Installation

### Add Marketplace

```bash
/plugin marketplace add pknull/asha-marketplace
```

### Install Panel Plugin

```bash
/plugin install panel-system@asha-marketplace
```

### Verify Installation

```bash
/plugin list
/panel  # Test panel command
```

---

## Plugin Directory Structure

```
asha-marketplace/
├── .claude-plugin/
│   └── marketplace.json          # Marketplace metadata
├── plugins/                       # Plugin collection
│   └── panel/                     # Panel system plugin
│       ├── .claude-plugin/
│       │   └── plugin.json
│       ├── commands/
│       │   └── panel.md
│       ├── agents/
│       │   └── recruiter.md
│       ├── docs/
│       │   └── characters/        # 3 core role profiles
│       │       ├── Asha.md
│       │       ├── The Adversary.md
│       │       └── The Recruiter.md
│       ├── README.md
│       └── LICENSE
└── README.md                      # This file
```

---

## Future Plugins

This marketplace will expand with additional Asha framework tools:

- **TTRPG Session Management** (coming soon)
- **Character Development Tools** (planned)
- **Narrative Analysis** (planned)
- **World Consistency Validation** (planned)

---

## Prerequisites

### Panel System Requirements

1. **AGENTS.md Framework** (required):
   - Authority Hierarchy (DAG)
   - Mode Selection protocols
   - Agent deployment framework

2. **Memory Bank Architecture** (recommended):
   - `Memory/activeContext.md`
   - `Memory/projectbrief.md`
   - Session continuity integration

3. **agent-fabricator** (recommended):
   - Enables dynamic agent creation during panels
   - Falls back to 239-agent library if unavailable

**[See panel prerequisites →](plugins/panel/README.md#prerequisites)**

---

## About Asha

Asha is a framework for AI-assisted creative writing, TTRPG campaign management, and narrative development. The framework provides:

- **Memory Bank** for session continuity
- **Panel System** for structured multi-perspective analysis
- **Agent Ecosystem** with 239+ specialized agents
- **TTRPG Integration** for Redacted107 universe
- **Workflow Automation** for creative projects

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

---

## Support

Issues and feature requests: https://github.com/pknull/asha-marketplace/issues

---

## Version History

### v1.1.0 (2025-11-08)
- Panel system v4.1.0 with dynamic recruitment architecture
- 3 core roles (Asha, Recruiter, Adversary) + recruited specialists
- Simplified invocation: `/panel <topic>` handles everything automatically
- The Adversary now argues against proposals by default (opposition stance)
- Session-specific naming for recruited specialists
- Removed 5 universal character profiles (simplified to core 3)
- Conventional marketplace structure with `plugins/` directory

### v1.0.0 (2025-11-08)
- Initial marketplace release
- Panel system v3.0.2 included
- Universal composition with abstention protocol
- Dynamic agent assignment via The Recruiter
