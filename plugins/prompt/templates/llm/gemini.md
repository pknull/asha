# Gemini 2.x / Gemini 3 Pro

## Strengths

- Strong at long-context (1M token window) and multimodal tasks
- Gemini 3 Pro powers Antigravity — excellent at frontend code

## Common Issues

Prone to hallucinated citations. Add:
> "Cite only sources you are certain of. If uncertain, say [uncertain] rather than guessing."

Can drift from strict output formats. Use explicit format locks with labelled example.

## Best Practices

- Leverage long-context for document-heavy prompts
- For grounded tasks: "Base response only on provided context. Do not extrapolate."
- Use explicit format lock with example
