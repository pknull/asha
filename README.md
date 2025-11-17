# asha-marketplace

**Version**: 1.2.0
**Description**: Claude Code plugins for multi-perspective analysis, session management, and collaborative decision-making

A collection of general-purpose Claude Code plugins for technical teams, creative projects, and systematic decision-making workflows.

---

## Available Plugins

### Panel System

**Plugin Name**: `panel-system`
**Command**: `/panel`
**Version**: 4.1.2
**Description**: Dynamic multi-perspective analysis with automatic specialist recruitment

Convene a structured analysis panel with 3 core roles (Moderator, Analyst, Challenger) + dynamically recruited specialists who examine your topic from distinct perspectives and produce evidence-based decisions.

**Use Cases**:
- Technical architecture decisions
- Code review and security analysis
- Creative writing evaluation
- Strategic planning
- Research methodology
- Ethical assessment

**[Full Documentation →](plugins/panel/README.md)**

### Memory+Session Manager

**Name**: `memory-session-manager`
**Version**: 1.0.0
**Description**: Memory file maintenance and session capture for multi-session AI projects

Provides automatic session watching, guided synthesis workflows, and Memory file structure specifications for maintaining context across discontinuous Claude Code sessions.

**Use Cases**:
- Long-term software projects
- Multi-session creative work
- Persistent context management
- Session continuity across teams

**[Full Documentation →](plugins/memory-session-manager/README.md)**

---

## Installation

### Add Marketplace

```bash
/plugin marketplace add pknull/asha-marketplace
```

### Install Plugins

```bash
<<<<<<< HEAD
# Install panel system
/plugin install panel@asha-marketplace

# Install memory+session manager
/plugin install memory-session-manager@asha-marketplace
=======
/plugin install panel-system@asha-marketplace
>>>>>>> claude/fix-issue-1-01Gyt9DQqudRHAWgmkPpi35D
```

### Verify Installation

```bash
/plugin list

# Test commands
/panel
/save
/silence status
```

---

## Quick Start

### Panel System

```bash
/panel Should we implement GraphQL or REST for the new API
/panel Review authentication system for OWASP Top 10 vulnerabilities
/panel Evaluate Chapter 9's prose quality and narrative effectiveness
```

The panel automatically:
- Analyzes topic to determine needed expertise
- Recruits 2-5 specialist agents from your agent library
- Assigns specialists with contextual session-specific names
- Executes 11-phase structured decision protocol
- Produces comprehensive Decision Report

### Memory+Session Manager

```bash
# Session watching runs automatically via hooks
# At end of session:
/save

# Toggle logging for experimental work:
/silence on    # Disable logging
/silence off   # Re-enable logging
```

---

## Plugin Directory Structure

```
asha-marketplace/
├── .claude-plugin/
│   └── marketplace.json          # Marketplace metadata
├── plugins/
│   ├── panel/                     # Panel system plugin
│   │   ├── .claude-plugin/
│   │   │   └── plugin.json
│   │   ├── commands/
│   │   │   └── panel.md
│   │   ├── agents/
│   │   │   └── recruiter.md
│   │   ├── docs/
│   │   │   └── characters/        # 3 core role profiles
│   │   │       ├── The Moderator.md
│   │   │       ├── The Analyst.md
│   │   │       └── The Challenger.md
│   │   ├── README.md
│   │   └── LICENSE
│   └── memory-session-manager/    # Session management plugin
│       ├── .claude-plugin/
│       │   └── plugin.json
│       ├── commands/
│       │   ├── save.md
│       │   └── silence.md
│       ├── hooks/
│       │   ├── hooks.json
│       │   ├── common.sh
│       │   ├── post-tool-use
│       │   ├── user-prompt-submit
│       │   └── session-end
│       ├── scripts/
│       │   └── save-session.sh
│       ├── skills/
│       │   └── memory-maintenance/
│       ├── docs/
│       └── README.md
└── README.md                      # This file
```

---

## Prerequisites

### Panel System Requirements

1. **Agent Library** (recommended):
   - Available agents in `.claude/agents/*.md`
   - Panel recruits specialists from available agents
   - More agents = better specialist matching

2. **Memory Bank Architecture** (optional but recommended):
   - `Memory/activeContext.md` for session continuity
   - Decision logging integration

3. **agent-fabricator** (optional):
   - Enables dynamic agent creation when capability gaps detected
   - Falls back to available agent library if unavailable

**[See panel prerequisites →](plugins/panel/README.md#prerequisites)**

### Memory+Session Manager Requirements

**Minimum** (all platforms):
- Persistent text storage
- Ability to read/update files

**Enhanced** (Claude Code):
- File system access (Memory/sessions/ directory)
- Bash execution (save-session.sh)
- Hooks (automatic capture)
- Git integration (commits)

---

## Philosophy

### Panel System

**Structured Collaborative Inquiry**:
- 3 core roles provide consistent perspective framework
- Dynamic specialist recruitment matches expertise to topic
- 11-phase protocol ensures systematic analysis
- Evidence-based decisions with confidence scoring

**Universal Application**:
- Technical: Architecture, security, performance
- Creative: Narrative, craft, consistency
- Strategic: Planning, resource allocation, risk
- Ethical: Privacy, bias, impact assessment

### Memory+Session Manager

**Separation of Concerns**:
- Your framework tells Claude to READ Memory
- This plugin tells Claude HOW TO MAINTAIN Memory

**Session Continuity**:
- Every session begins fresh (Claude context resets)
- Memory is the ONLY connection to previous work
- Session watching captures operations automatically
- Synthesis transforms operations into persistent context

**Cross-Platform**:
- Core protocol works anywhere (manual synthesis)
- Enhanced automation on capable platforms (Claude Code)
- Graceful degradation strategy

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
- **Memory+Session Manager**: MIT License

---

## Support

**Issues and feature requests**: https://github.com/pknull/asha-marketplace/issues

**Documentation**:
- Panel system: `plugins/panel/README.md`
- Memory manager: `plugins/memory-session-manager/README.md`
- Development guide: `CLAUDE.md`

---

## Version History

### v1.2.0 (2025-11-17)
- **Sterilization**: Removed universe-specific references, made plugins general-purpose
- Renamed character files to universal archetypes (Moderator, Analyst, Challenger)
- Updated all documentation for broader applicability
- Generalized keywords and descriptions
- Maintained marketplace name and plugin functionality

### v1.1.0 (2025-11-08)
- Panel system v4.1.0 with dynamic recruitment architecture
- 3 core roles + recruited specialists with session-specific naming
- Simplified invocation: `/panel <topic>` handles everything automatically
- Opposition stance for Challenger role (argues against proposals by default)
- Removed 5 universal character profiles (simplified to core 3)
- Conventional marketplace structure with `plugins/` directory

### v1.0.0 (2025-11-08)
- Initial marketplace release
- Panel system v3.0.2 included
- Universal composition with abstention protocol
- Dynamic agent assignment via The Analyst
