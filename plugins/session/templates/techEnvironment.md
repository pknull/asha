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
| `/session:save` | Save session context, archive, commit |
| `/session:init` | Initialize session management + identity |

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

## Verification

Commands run by `/code:verify`:

| Command | Purpose |
|---------|---------|
| `[test command]` | Run test suite |
| `[lint command]` | Check code style |

<!-- If no commands defined, /code:verify will detect and propose based on project type -->
