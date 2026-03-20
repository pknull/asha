# Qwen 2.5 / Qwen3

## Qwen 2.5 (Instruct)

- Excellent instruction following, JSON output, structured data
- 128K context window
- Clear system prompt defining role works well
- Works with explicit output format including JSON schemas
- Multilingual — specify output language if not obvious
- Chat template: system message + user message, not single blob
- Shorter focused prompts outperform long complex ones

## Qwen3 (Thinking Mode)

**Two modes:**

- Thinking mode (`/think` prefix or `enable_thinking=True`) — like o1, reasons internally
- Non-thinking mode — like standard LLM

**In thinking mode:** Treat like o1. Short clean instructions. No CoT. No scaffolding.

**In non-thinking mode:** Full structure, explicit format, role assignment. Temperature 0.7, TopP 0.8.

User can switch mid-conversation with `/think` or `/no_think`.
