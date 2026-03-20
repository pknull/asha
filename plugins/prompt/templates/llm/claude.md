# Claude (claude.ai, API, Claude 4.x)

## Best Practices

- Be explicit and specific — Claude responds to precise instructions, not hints
- XML tags useful for multi-component prompts: `<context>`, `<task>`, `<constraints>`, `<examples>`, `<output_format>`
- Provide WHY not just WHAT — Claude generalizes better from explanations
- Use `<examples>` tags for few-shot — 3-5 examples improve format consistency
- Explicit output format beats vague requests

## Opus-Specific

Claude Opus over-engineers by default. Add:
> "Keep solutions minimal. Only make changes directly requested. Do not add features, refactor, or improve beyond what was asked."

## Avoid

- Over-constraining — Claude infers well from clear context
- Vague adjectives — be specific about format, length, style
