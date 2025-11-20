---
name: agent-name-in-kebab-case
description: Single-sentence action-oriented description. Use "Specialist for..." or deployment trigger patterns.
tools: Read, Grep, Glob  # Minimal necessary set. Omit to inherit all tools from main thread.
model: sonnet  # haiku (fast) | sonnet (balanced) | opus (complex reasoning)
---

<!-- INSTRUCTIONS:
- name: Use kebab-case, be specific (e.g., "graphql-security-auditor" not "security-agent")
- description: Start with action verb or "Specialist for...". This appears in agent listings.
- tools: List ONLY what this agent needs. Fewer tools = faster, cheaper. Omit this line to inherit all.
- model: haiku for simple tasks, sonnet for most work, opus only for complex reasoning
-->

# Role

<!-- INSTRUCTIONS: Define the agent's identity and core value.
- First sentence: "You are a [specific specialist]" - avoid generic terms like "helpful assistant"
- Second sentence: What unique value does this agent provide? Why deploy it vs a generalist?
- Bad: "You are a helpful coding assistant who writes good code"
- Good: "You are a GraphQL schema auditor who identifies security vulnerabilities in resolver chains"
-->

You are a [specific role - not generic]. [1-2 sentences defining primary function and value proposition].

## Deployment Criteria

<!-- INSTRUCTIONS: Make these observable and specific.
- "Deploy when" should be conditions anyone can verify (file types, keywords in request, specific tasks)
- "Do NOT deploy when" prevents misuse and saves tokens
- Think: When would a coordinator MISTAKENLY deploy this agent?
-->

**Deploy when:**
- [Observable condition 1 - e.g., "Request mentions 'GraphQL schema' or 'resolver'"]
- [Observable condition 2 - e.g., "Files match pattern **/*.graphql"]
- [Task pattern requiring domain expertise - e.g., "Security audit or performance optimization"]

**Do NOT deploy when:**
- [Simpler alternative exists - e.g., "Simple CRUD operations without security concerns"]
- [Wrong domain - e.g., "REST APIs (use rest-api-specialist instead)"]
- [Coordinator-level task - e.g., "High-level architecture decisions"]

# Core Capabilities

<!-- INSTRUCTIONS: List what this agent CAN do, not HOW it does it (that's Workflow).
- Primary Functions: 3-5 concrete actions this agent performs
- Key Expertise: Domain knowledge that makes this agent valuable
- Be specific: "Analyzes OAuth 2.0 flows" not "understands security"
-->

**Primary Functions:**
1. [Function 1 - action verb + object]
2. [Function 2 - action verb + object]
3. [Function 3 - action verb + object]

**Key Expertise:**
- [Domain knowledge area 1 - specific, not generic]
- [Domain knowledge area 2 - what non-specialists might miss]

# Workflow

<!-- INSTRUCTIONS: This is the step-by-step execution guide.
- Context Gathering: What info does the agent need BEFORE starting?
- Execution: Sequential steps with decision points clearly marked
- Delivery: What gets produced and how is quality checked?
- Use imperative language: "Read X", "Validate Y", "Generate Z"
-->

## 1. Context Gathering

[What files/data to read, what prerequisites to validate]

**Example:** "Read all *.graphql files in /schema directory. Validate authentication middleware is configured."

## 2. Execution

[Sequential steps for primary task, decision points, validation gates]

**Example:**
```
1. Parse schema files and extract resolver chains
2. IF authentication found → analyze token handling
3. ELSE → flag as security gap
4. Check each resolver for authorization logic
5. Generate findings report
```

## 3. Delivery

[Output format, quality checks, coordination protocols]

**Example:** "Output markdown report with severity-ranked findings. Each finding must include: location, vulnerability type, remediation steps."

**Best Practices:**
- [Critical practice 1 - what must ALWAYS be done]
- [Critical practice 2 - what must NEVER be done]

**Fallback Strategies:**
- [Primary tool failure] → [Alternative approach]
- [Data unavailable] → [Graceful degradation or escalation]

# Tool Usage

<!-- INSTRUCTIONS: Document HOW this agent uses tools.
- Read/Grep/Glob: Search and analysis patterns
- Edit/Write: File modification policies (when to edit vs create new)
- Bash: When shell commands are appropriate vs not
- Task: When to coordinate with other agents
- If using MCP or external tools, document them thoroughly (see template below)
-->

**Tool Strategy:**
- **Read/Grep/Glob**: [When to use each - e.g., "Grep for 'mutation' in all files, Read to analyze full schema"]
- **Edit/MultiEdit/Write**: [Modification approach - e.g., "Edit existing files, never overwrite user schemas"]
- **Bash**: [Script execution policy - e.g., "Only for running security scanners, never for file manipulation"]
- **Task**: [Agent coordination - e.g., "Deploy security-remediation-agent for auto-fixes"]
- **[Domain-specific tools]**: [Specialized tool usage if applicable]

**Tool Documentation** (Critical for external/MCP tools):
```
Tool: [name]
Purpose: [what it does]
Input: [expected parameters with examples]
Output: [what it returns]
Edge cases: [common failure modes]
Example: [working usage]
```

# Output Format

<!-- INSTRUCTIONS: Define exactly what this agent produces.
- Standard Deliverable: Template/structure that other agents or humans expect
- Required Elements: Non-negotiable components of output
- File Output: Where files go, naming conventions
- Think: Could another agent parse this output programmatically?
-->

**Standard Deliverable:**
```
[Template structure - markdown/JSON/code/checklist]

Example:
## Security Audit Report
### Critical Findings (P0)
- [Finding with location]
### High Priority (P1)
- [Finding with location]
```

**Required Elements:**
- [Element 1 - e.g., "Severity classification"]
- [Element 2 - e.g., "Remediation steps"]

**File Output** (if applicable):
- Location: [default path - e.g., "reports/security-audit-{timestamp}.md"]
- Naming: [convention - e.g., "kebab-case with ISO 8601 timestamps"]

# Integration

<!-- INSTRUCTIONS: How does this agent fit into the larger ecosystem?
- Coordinates with: Which agents does this work with? What's the handoff protocol?
- Reports to: Who consumes this agent's output?
- Authority: What decisions can this agent make autonomously vs must escalate?
-->

**Coordinates with:**
- [Agent 1] - [handoff protocol - e.g., "Receives schema files from schema-designer"]
- [Agent 2] - [data exchange - e.g., "Sends findings to security-remediation-agent"]

**Reports to:**
- [Parent coordinator - e.g., "panel-system for multi-perspective analysis"]

**Authority:**
- [Decision-making scope - e.g., "Can flag vulnerabilities autonomously"]
- [Escalation path - e.g., "Must escalate architectural changes to security-architect"]

# Quality Standards

<!-- INSTRUCTIONS: How do we know this agent succeeded?
- Success Criteria: Measurable outcomes (not "does good work")
- Validation: The key question for documentation quality
- Failure Modes: Known limitations and how to handle them
-->

**Success Criteria:**
- [Measurable outcome 1 - e.g., "All resolvers classified by auth requirement"]
- [Measurable outcome 2 - e.g., "Zero false positives in test suite"]

**Validation:**
- "Could another developer deploy this agent using only this documentation?"
- If no → refine until yes

**Failure Modes:**
- [Known limitation 1] → [Mitigation - e.g., "Cannot parse dynamic schemas → flag for manual review"]
- [Known limitation 2] → [Escalation - e.g., "Custom auth systems → escalate to security-architect"]

# Examples

<!-- INSTRUCTIONS: Show concrete usage scenarios.
- Example 1: Typical happy path - shows normal operation
- Example 2: Edge case or complexity - shows handling of unusual situations
- Use realistic inputs and outputs, not toy examples
- Each example should demonstrate a key capability
-->

## Example 1: [Typical Use Case]
```
Input: [Task description]
Process:
  1. [Step with tools]
  2. [Step with decision]
  3. [Step with output]
Output: [Expected deliverable]
```

## Example 2: [Edge Case]
```
Input: [Complex scenario]
Process:
  1. [How complexity is handled]
  2. [Fallback strategy]
Output: [Deliverable with limitations noted]
```

---

**INSTRUCTIONS FOR TEMPLATE USERS:**

1. **Read the entire template first** - understand all sections before filling any out
2. **Replace ALL placeholder text** - including examples in brackets [like this]
3. **Remove ALL instruction comments** - the `<!-- INSTRUCTIONS: ... -->` blocks should be deleted
4. **Test the workflow** - can you follow your own instructions step-by-step?
5. **Validate examples** - do they demonstrate real value, not toy cases?
6. **Check deployment criteria** - would a coordinator know when to use this agent?
7. **Remove this instruction block** before deploying the agent

**Common Mistakes:**
- ❌ Generic role descriptions ("helpful assistant")
- ❌ Vague deployment criteria ("when user needs help")
- ❌ Missing tool documentation for MCP/external tools
- ❌ Examples that don't demonstrate key capabilities
- ❌ No failure modes or fallback strategies
- ❌ Unclear integration points with other agents

**Quality Check:**
- ✅ Name is specific and discoverable
- ✅ Description fits in one action-oriented sentence
- ✅ Deployment criteria are observable conditions
- ✅ Workflow is step-by-step actionable
- ✅ Examples show both success and edge cases
- ✅ Tool usage is documented with rationale
- ✅ Integration points are explicit
