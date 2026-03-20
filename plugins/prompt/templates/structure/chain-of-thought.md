# Chain of Thought Template

Use for logic-heavy tasks, math, debugging, multi-factor analysis.

## Critical Warning

**Only for standard models (Claude, GPT-4o, Gemini, Qwen2.5, Llama).**
**NEVER for o1, o3, DeepSeek-R1, Qwen3-thinking — they reason internally, CoT degrades output.**

## Structure

```
[Task statement]

Before answering, think through this carefully:
1. What is the actual problem being asked?
2. What constraints must the solution respect?
3. What are the possible approaches?
4. Which approach is best and why?

Give your final answer only.
```

## When to Use

- Debugging where cause is not obvious
- Comparing technical approaches
- Math or calculations
- Analysis where wrong first impression is likely

## When NOT to Use

- o1/o3/reasoning models
- Simple tasks with clear answers
- Creative tasks (CoT kills natural voice)
