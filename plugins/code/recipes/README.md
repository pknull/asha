# Swarm Recipes

Reusable multi-agent workflow definitions. Each recipe encodes a complete workflow as an executable pattern.

## Usage

Recipes are reference documents, not automated scripts. When starting a task that matches a recipe:

1. Read the recipe to understand the workflow
2. Follow the agent sequence and transitions
3. Respect checkpoints (human approval gates)
4. Handle failures per the recipe's failure policy

## Recipe Schema

```yaml
name: recipe-name
version: "1.0"
description: What this recipe accomplishes
complexity: quick | standard | full  # Maps to Scale-Adaptive Planning

agents:
  - name: agent-name
    role: what they do
    model: haiku | sonnet | opus
    trigger: "@state-that-activates"
    output: "@state-when-done"
    checkpoint: true | false  # Human approval required after this agent?
    tools: [list, of, tools]  # Optional constraints

flow:
  start: "@initial-state"
  end: "@final-state"

  transitions:
    # Agent transition
    - from: "@state-a"
      to: "@state-b"
      agent: agent-name
      condition: optional condition

    # Checkpoint transition (human approval gate)
    - from: "@pending-approval"
      to: "@approved"
      type: checkpoint
      description: "Human reviews and approves"

    # Action transition (merge, deploy, etc.)
    - from: "@ready"
      to: "@done"
      type: action
      description: "Final action to complete workflow"

failure:
  max_retries: 3
  on_blocked: escalate_to_user | skip | abort
  record_to_reasoning_bank: true
```

### Transition Types

| Type | Description |
|------|-------------|
| (default) | Agent processes and advances state |
| `checkpoint` | Human approval required to advance |
| `action` | Non-agent action (merge, deploy) |

## State Labels

States use `@label` format (inspired by Pied-Piper). Common states across recipes:

| Label | Meaning |
|-------|---------|
| `@needs-research` | Requires codebase-historian |
| `@research-complete` | Prior art gathered |
| `@needs-design` | Requires architecture/planning |
| `@design-ready` | Design complete, awaiting approval |
| `@design-approved` | Human approved design (checkpoint) |
| `@implementation-ready` | Code complete |
| `@changes-requested` | Review failed, needs fixes |
| `@review-passed` | Review approved |
| `@ready-to-merge` | All gates passed |
| `@merged` | Complete |

### Recipe-Specific States

**bug-investigation:**
- `@bug-reported` → `@history-checked` → `@root-cause-found` → `@root-cause-confirmed` (checkpoint) → `@regression-test-written` → `@fix-implemented`

**refactor-safe:**
- `@refactor-requested` → `@patterns-checked` → `@refactor-plan-ready` → `@refactor-approved` (checkpoint) → `@refactor-complete`

**security-audit:**
- `@audit-requested` → `@history-reviewed` → `@vulnerabilities-found` → `@findings-reviewed` (checkpoint) → `@remediation-plan-ready` → `@remediation-approved` (checkpoint) → `@fixes-implemented` → `@verification-passed` → `@audit-complete`

## Available Recipes

| Recipe | Complexity | Use Case |
|--------|------------|----------|
| `feature-implementation.yaml` | full | New features end-to-end |
| `bug-investigation.yaml` | standard | Bug diagnosis and fix |
| `refactor-safe.yaml` | standard | Code cleanup with safety |
| `security-audit.yaml` | full | Security hardening |

## Customizing Recipes

Copy a recipe and modify:
- Swap agents for project-specific alternatives
- Add/remove checkpoints based on risk tolerance
- Adjust failure policies
- Change model assignments (opus for critical steps, haiku for simple)

## Integration with Orchestration Module

Recipes implement the patterns from `modules/orchestration.md`:
- **Quality Gates** → Checkpoints in recipe flow
- **Scale-Adaptive** → Recipe complexity field
- **Two-Stage Review** → Separate spec-compliance and code-quality agents
- **Failure Tracking** → Recipe failure policy
