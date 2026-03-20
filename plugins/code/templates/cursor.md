# Cursor / Windsurf Prompt Template

## Critical Rules

- ALWAYS include file path
- ALWAYS include "Do NOT modify [list]"
- Specify language/framework version
- Include "Done when:" condition

## Structure

```
File: [exact/path/to/file.ext]
Function/Component: [exact name]

Current Behavior:
[What code does now]

Desired Change:
[What it should do after edit]

Scope:
Only modify [function/component].
Do NOT touch: [list everything to leave unchanged]

Constraints:
- Language/framework: [version]
- No new dependencies
- Preserve existing [signatures/contracts]

Done When:
[Condition that confirms change worked]
```

## Example

```
File: src/components/LoginForm.tsx
Function: handleSubmit

Current Behavior:
Submits form synchronously, no loading state.

Desired Change:
Add loading state during API call, disable button while loading.

Scope:
Only modify handleSubmit function and button JSX.
Do NOT touch: form validation, styling, other handlers

Constraints:
- React 18, TypeScript strict
- No new dependencies
- Preserve existing onSuccess callback

Done When:
Button shows "Loading..." and is disabled during submission.
```

## Common Mistakes

- No file path (edits wrong file)
- No "Do NOT modify" list (unintended changes)
- Vague "make it better" (no clear end state)
- Missing version constraints
