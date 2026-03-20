# GPT-4 / GPT-4o

## Best Practices

- Strong role assignment in system prompt calibrates entire response
- Numbered instructions and explicit step sequences work well
- Use numeric constraints over adjectives: "under 100 words" not "concise"
- Specify exact format with labelled example for structured output

## Common Issues

GPT-4o adds filler and caveats. Add:
> "Skip preamble. No caveats. Answer directly."

GPT-4o is verbose by default. Always set length cap.

## Format

```
System: [Role assignment]
User: [Task with numbered steps if complex]
```
