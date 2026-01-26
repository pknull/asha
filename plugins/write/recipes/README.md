# Writing Recipes

Reusable multi-agent workflows for creative writing. Each recipe encodes a complete workflow as an executable pattern.

## Usage

Recipes are reference documents for orchestrating writing agents. When starting a writing task:

1. Read the recipe matching your task type
2. Follow the agent sequence and state transitions
3. Respect checkpoints (human approval gates)
4. Handle failures per the recipe's failure policy

## Recipe Schema

```yaml
name: recipe-name
version: "1.0"
description: What this recipe accomplishes
complexity: quick | standard | full

agents:
  - name: agent-name
    role: what they do
    model: haiku | sonnet | opus
    trigger: "@state-that-activates"
    output: "@state-when-done"
    checkpoint: true | false

flow:
  start: "@initial-state"
  end: "@final-state"

  transitions:
    - from: "@state-a"
      to: "@state-b"
      agent: agent-name

    - from: "@pending-approval"
      to: "@approved"
      type: checkpoint
      description: "Human reviews and approves"

failure:
  max_retries: 3
  on_blocked: escalate_to_user
```

## State Labels

States use `@label` format. Common writing states:

| Label | Meaning |
|-------|---------|
| `@needs-outline` | Requires structure before prose |
| `@outline-ready` | Structure complete, awaiting approval |
| `@outline-approved` | Human approved structure |
| `@needs-draft` | Ready for prose generation |
| `@draft-complete` | First draft exists |
| `@needs-dev-edit` | Awaiting structural review |
| `@structure-approved` | Developmental edit passed |
| `@needs-line-edit` | Ready for prose polish |
| `@line-edit-complete` | Prose polished |
| `@ready-to-publish` | All gates passed |
| `@published` | Complete |

### Recipe-Specific States

**chapter-creation:**
`@chapter-requested` → `@outline-ready` → `@outline-approved` → `@draft-complete` → `@structure-approved` → `@line-edit-complete` → `@ready-to-publish`

**manuscript-revision:**
`@revision-requested` → `@dev-edit-complete` → `@structure-approved` → `@line-edit-complete` → `@ready-to-publish`

**character-development:**
`@character-requested` → `@concept-ready` → `@backstory-complete` → `@voice-tested` → `@character-approved`

## Available Recipes

| Recipe | Complexity | Use Case |
|--------|------------|----------|
| `chapter-creation.yaml` | standard | New chapter from concept to polish |
| `manuscript-revision.yaml` | full | Complete revision of existing draft |
| `character-development.yaml` | standard | Deep character creation |

## Customizing Recipes

Copy a recipe and modify:
- Swap agents for project-specific alternatives
- Add/remove checkpoints based on workflow needs
- Adjust failure policies
- Change model assignments (opus for critical creative decisions)
