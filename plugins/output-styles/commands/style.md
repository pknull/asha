---
description: "Switch output styles for Claude Code responses"
argument-hint: "[style-name] | off"
allowed-tools: ["Read", "Write", "Bash", "Glob"]
---

# /style

Switch between output styles for Claude Code responses.

## Usage

```
/style                    # List available styles and show current
/style <name>             # Switch to a specific style
/style off                # Disable output styling
```

## Execution

### List Styles (no argument)

Read all `.md` files from `${CLAUDE_PLUGIN_ROOT}/styles/` directory and display:

```markdown
## Available Output Styles

**Current**: {active style or "none"}

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

Usage: `/style <name>` to switch, `/style off` to disable
```

### Switch Style

1. Validate the style name exists in `${CLAUDE_PLUGIN_ROOT}/styles/{name}.md`
2. Write the style name to `~/.claude/active-output-style`
3. Confirm the switch:

```markdown
Switched to **{name}** output style.

{one-line description from style file}

Style will be active for new sessions. Restart or use `/clear` to apply.
```

### Disable Style

When argument is `off`:
1. Remove `~/.claude/active-output-style` file
2. Confirm: "Output styling disabled. Default formatting will be used."

## Notes

- Style persists across sessions via config file
- SessionStart hook reads config and injects style instructions
- Style files use YAML frontmatter for metadata (name, description)
