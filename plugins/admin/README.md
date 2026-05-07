# Admin Plugin

**Version**: 0.1.0

Personal admin integrations — task managers, calendars, knowledge bases, and other "life logistics" tools. Each integration is a self-contained skill that calls its service's REST API directly, bypassing MCP server fragility.

## Philosophy

For thin REST APIs you control (your own tokens, simple endpoints), MCP wrappers add a daemon to maintain without adding capability. This plugin captures the bypass pattern: skill knows the API shape, user provides the token via env var, model invokes via `curl`.

When to keep MCP instead:
- Rich tool surface (Gmail's filters/labels/threads vocabulary)
- Vendor maintains the wrapper
- High-frequency operations where typed tools save tokens

When to use this plugin:
- You own the API token
- The endpoint is shallow (few REST calls cover the use case)
- MCP server reliability has been a recurring friction point

## Skills

### todoist

Direct Todoist REST API access. Handles task creation, search, completion, and update — full read+write coverage that the read-only MCP agent can't provide.

**Triggers**: Todoist task creation, due-date queries, marking tasks complete, project/label management.

**Requires**: `TODOIST_API_TOKEN` env var.

## Usage

Each skill is invoked by name (`/admin-todoist`, `/admin-gemini`, `/admin-bookstack`). The model picks the skill when the conversation matches its trigger description and routes the request to the right curl recipe. No explicit invocation is needed for the common cases — say "remind me to X" and the todoist skill engages automatically.

Examples:

- "Add a Todoist task for the dentist next Tuesday at 10am p2."
- "What's overdue in Todoist?"
- "Mark task 6gVq6vw4W5rHC5ww complete."

Each skill documents its own API recipes; see `skills/<name>/SKILL.md` for the full surface.

## Adding a new integration

Each integration is its own skill directory under `skills/`. They don't reference each other — removing or replacing one doesn't ripple to siblings. To add (e.g.) `gws` or `bookstack`:

```
plugins/admin/skills/<name>/SKILL.md
```

Document the API endpoints, the env-var token requirement, and a handful of curl recipes for the common operations. That's the whole shape.

## Installation

Installed via the asha symlink-mount installer:

```bash
./install.sh --only admin
```

## License

MIT
