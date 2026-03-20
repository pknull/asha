# Few-Shot Template

Use when output format is easier to show than describe. Examples outperform instructions for format-sensitive tasks.

## Structure

```
[Task instruction]

Here are examples of the exact format needed:

<examples>
  <example>
    <input>[example input 1]</input>
    <output>[example output 1]</output>
  </example>
  <example>
    <input>[example input 2]</input>
    <output>[example output 2]</output>
  </example>
</examples>

Now apply this exact pattern to: [actual input]
```

## Rules

- 2-5 examples is sweet spot. More rarely helps, wastes tokens.
- Include edge cases, not just easy cases.
- XML tags for wrapping — Claude parses reliably.
- If re-prompting same format issue twice, switch to few-shot.
