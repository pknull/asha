# Output Styles

**Version**: 1.0.1
**Command**: `/style`

Switchable output styles for Claude Code responses.

## Installation

```bash
/plugin marketplace add pknull/asha-marketplace
/plugin install output-styles@asha-marketplace
```

## Usage

```bash
/style                    # List available styles and show current
/style <name>             # Switch to a specific style
/style off                # Disable output styling
```

## Available Styles

| Style | Description |
|-------|-------------|
| `ultra-concise` | Minimal words, direct actions |
| `bullet-points` | Hierarchical bullet points |
| `genui` | Generative UI with HTML output |
| `html-structured` | Clean semantic HTML |
| `markdown-focused` | Full markdown features |
| `table-based` | Table-based organization |
| `tts-summary` | Audio TTS announcements |
| `yaml-structured` | YAML structured output |

## How It Works

1. **Selection**: `/style <name>` writes the style to `~/.claude/active-output-style`
2. **SessionStart Hook**: Reads config and injects style instructions into context
3. **Persistence**: Style persists across sessions until changed

## Examples

```bash
# For quick terminal work
/style ultra-concise

# For documentation
/style markdown-focused

# For data-heavy responses
/style table-based

# Reset to default
/style off
```

## Creating Custom Styles

Add `.md` files to `styles/` directory with YAML frontmatter:

```yaml
---
name: my-style
description: Brief description
---

Style instructions here...
```

## License

MIT License
