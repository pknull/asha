---
version: "1.0"
lastUpdated: "YYYY-MM-DD"
---

# techEnvironment

## Platform

**OS**: [Linux/macOS/Windows]
**Working Directory**: [path]

## Asha Framework

Tools are provided by the Asha plugin. Tool paths are injected via SessionStart hook.

### Available Commands

| Command | Purpose |
|---------|---------|
| `/asha:save` | Save session context, archive, refresh index, commit |
| `/asha:index` | Index files for semantic search |
| `/asha:init` | Initialize Asha in a new project |
| `/asha:cleanup` | Remove legacy asha/ installation files |

### Tool Invocation

Tools are executed via the plugin's Python environment. Example patterns provided in session context.

**Semantic Search**: Query indexed files using memory_index.py
**Pattern Tracking**: Track and query patterns via reasoning_bank.py

## Project-Specific Stack

[Add your project's technical details here]

### Languages & Frameworks

- [Language]: [Version]
- [Framework]: [Version]

### Dependencies

- [Key dependency]: [Purpose]

### Development Tools

- [Tool]: [Usage]
