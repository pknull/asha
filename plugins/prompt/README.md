# Prompt Plugin

**Version**: 0.1.0

Prompt-engineering toolkit for generating production-ready prompts for external AI tools (Midjourney, DALL-E, Cursor, GPT, Gemini, etc.).

## Contents

- `agents/engineer.md` — the prompt-engineer agent. Applies the anti-patterns checklist, selects from `templates/` based on target tool, and emits a single copyable prompt.
- `anti-patterns.md` — 35-entry reference of token-wasting and reprompt-causing patterns, with corrected forms.
- `templates/` — per-tool prompt skeletons (LLMs, image gen, structure-driven asks).

## Usage

The plugin is invoked indirectly via the `engineer` agent. Examples:

- "Write a Midjourney prompt for a Carcosan ritual chamber."
- "Optimize this Cursor prompt — it keeps producing wrong code."
- "Generate a structured GPT prompt for extracting JSON from invoices."

The agent reads the relevant template, applies the anti-patterns checklist, and returns a clean prompt formatted for the target tool. Don't deploy for direct task execution — if you want the work done, ask Claude to do it instead of writing a prompt about it.

## Installation

Installed via the asha symlink-mount installer:

```bash
./install.sh --only prompt
```

## License

MIT
