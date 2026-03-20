# Anti-Patterns Reference

35 patterns that waste tokens and cause re-prompts. Apply as checklist when generating prompts for external tools.

## Task Patterns

| # | Pattern | Bad | Fixed |
|---|---------|-----|-------|
| 1 | Vague task verb | "help me with my code" | "Refactor `getUserData()` to use async/await" |
| 2 | Two tasks in one | "explain AND rewrite" | Split into two prompts |
| 3 | No success criteria | "make it better" | "Done when tests pass and handles null" |
| 4 | Over-permissive agent | "do whatever it takes" | Explicit allowed + forbidden actions |
| 5 | Emotional description | "it's totally broken" | "Throws TypeError on line 43" |
| 6 | Build-the-whole-thing | "build my entire app" | Break into scaffold, core, polish |
| 7 | Implicit reference | "the thing we discussed" | Always restate full task |

## Context Patterns

| # | Pattern | Bad | Fixed |
|---|---------|-----|-------|
| 8 | Assumed prior knowledge | "continue where we left off" | Include memory block |
| 9 | No project context | "write a cover letter" | Include role, experience, target |
| 10 | Forgotten stack | Contradicts prior tech choice | Include established stack |
| 11 | Hallucination invite | "what do experts say?" | "Cite only sources you're certain of" |
| 12 | Undefined audience | "write for users" | "Non-technical B2B buyers" |
| 13 | No prior failures | (blank) | "Already tried X, didn't work" |

## Format Patterns

| # | Pattern | Bad | Fixed |
|---|---------|-----|-------|
| 14 | Missing output format | "explain this" | "3 bullets, under 20 words each" |
| 15 | Implicit length | "write a summary" | "Exactly 3 sentences" |
| 16 | No role assignment | (generic) | "Senior backend engineer" |
| 17 | Vague aesthetic | "make it professional" | "Monochrome, 16px font" |
| 18 | No negative prompts | "portrait of woman" | Add "no watermark, blur, extra fingers" |
| 19 | Prose for Midjourney | Full sentence | Comma-separated descriptors |

## Scope Patterns

| # | Pattern | Bad | Fixed |
|---|---------|-----|-------|
| 20 | No scope boundary | "fix my app" | "Fix only src/auth.js" |
| 21 | No stack constraints | "React component" | "React 18, TypeScript strict" |
| 22 | No stop condition | "build the feature" | Stop conditions + checkpoints |
| 23 | No file path (IDE) | "update login" | "Update src/pages/Login.tsx" |
| 24 | Wrong template | GPT prose in Cursor | Use file-scope template |
| 25 | Entire codebase | Full repo pasted | Scope to relevant file |

## Reasoning Patterns

| # | Pattern | Bad | Fixed |
|---|---------|-----|-------|
| 26 | No CoT for logic | "which is better?" | "Think through step by step" |
| 27 | CoT on reasoning models | "think step by step" to o1 | REMOVE — degrades output |
| 28 | Expecting memory | "you already know" | Re-provide context |
| 29 | Contradicting prior | Ignores architecture | Include memory block |
| 30 | No grounding | "summarize experts" | "Say [uncertain] if not sure" |

## Agentic Patterns

| # | Pattern | Bad | Fixed |
|---|---------|-----|-------|
| 31 | No starting state | "build REST API" | "Empty Node.js, Express installed" |
| 32 | No target state | "add auth" | "POST /login with JWT in src/routes" |
| 33 | Silent agent | No progress | "After each step output: [completed]" |
| 34 | Unlocked filesystem | No restrictions | "Only edit src/, don't touch config" |
| 35 | No human review | Agent decides all | "Stop before: delete, add deps, schema" |
