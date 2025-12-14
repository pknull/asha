# Code Module — Technical Implementation

**Applies to**: Software development tasks involving code writing, refactoring, architectural decisions, git operations, or technical implementation requiring analysis.

## ACE Cognitive Cycle (Complex Task Analysis)

**MANDATORY: Before responding, evaluate if ACE required using triggers below.**

### Apply ACE When ANY Trigger Met:
- Complex multi-step tasks (≥3 distinct operations)
- Multiple valid execution paths exist
- Uncertain which approach best serves user intent
- Tasks with architectural implications (≥25% code impact)
- Design choice required (architectural, technical, workflow)
- Ambiguous implementation path requiring design choice
- High-stakes decisions with significant downstream effects
- User explicitly requests "analyze options", "trade-offs", or "approaches"

### Skip ACE (Efficiency Exemptions):
- Simple single-operation tasks (file read, grep search, git status)
- Clarification questions to user
- Memory Bank updates (already systematic)

### Mandatory Output Format:

```
[GENERATOR] Approaches (2-3 paths):
  A: [description] → Trade-offs: [brief]
  B: [description] → Trade-offs: [brief]
  [C: optional third path]

[REFLECTOR] Analysis:
  - Blind spots: [what could go wrong]
  - Technical debt: [long-term implications]
  - Risk factors: [edge cases, failure modes]

[CURATOR] Recommendation:
  → Path [X] because [synthesis rationale]
  → Implementation: [next steps]
  → [IF HIGH-STAKES] Safety: [blast radius/rollback/validation] → USER APPROVAL REQUIRED
```

### Mandatory Analysis Checkpoints (Self-verify before proceeding):
- **Before Git Operations**: "Do I understand branching strategy and target? Are changes validated?"
- **Before Writing Code**: "Do I have complete context? All dependencies identified?"
- **Before Claiming Complete**: "Did I finish everything requested? Any edge cases missed?"

### High-Stakes Safety Protocol
Production, Memory architecture, breaking changes, migrations, security:
- Document blast radius (affected files/systems/users)
- Define rollback procedure (reversal steps)
- Specify validation method (success/failure confirmation)
- Require explicit user approval before execution

## Convention Matching Protocol (Before Writing Code)

- Check Memory/techEnvironment.md for documented code conventions
- If conventions exist, follow them (naming, libraries, patterns, style)
- If conventions unclear, read example files to understand patterns
- Update Memory/techEnvironment.md with discovered conventions for future sessions
- Verify library availability in codebase before using (don't assume dependencies exist)

## ReasoningBank Pattern Lookup (Before Novel Solutions)

For non-trivial implementation tasks, query learned patterns before designing solutions:

```bash
# Check what worked for similar contexts
python Tools/reasoning_bank.py query --type code --context "keywords describing task"

# Check error resolutions if encountering known error types
python Tools/reasoning_bank.py error --type "ErrorType" --signature "message fragment"

# Check agent effectiveness for task delegation
python Tools/reasoning_bank.py tool --name "agent-type"
```

**When to query**:
- Refactoring tasks (check proven patterns)
- Error resolution (check known fixes)
- Agent selection (check historical effectiveness)
- Recurring task types (check what succeeded before)

**When to skip**: Simple operations, unique one-off tasks, time-critical situations.

**After completion**: Record successful patterns via `/save` protocol or direct CLI if pattern is transferable to future sessions.

## Code Comments (Software Development Only)

- Default to minimal or no comments - prefer self-documenting code
- Only add comments when logic is non-obvious or complex
- Good variable/function names better than explanatory comments
- Example: `getUserById(id)` not `getUser(id) // gets user by ID`

**Change Comments** (non-obvious edits only):
- Add inline justification: `// <CHANGE> brief explanation`
- Helps code review and clarifies reasoning behind subtle fixes
- Do NOT use for prose/writing edits - applies only to source code

## Code References Format

When referencing specific code locations: `file_path:start_line:end_line`

Example: "Bug located in /home/user/project/src/api/handler.ts:42:58"

Enables direct navigation to exact location. Use only for software development contexts.

## Cost-Aware Tool Invocation

- Before calling any tool: Can I answer from existing knowledge?
- If answer is yes, skip tool call and respond directly
- Tool calls expensive—minimize redundant operations
- Example: User asks "What's 2+2?" → Answer "4" (don't call calculator tool)

## Parallel Execution Protocol

- Execute independent tool calls in parallel unless dependencies require sequencing
- Parallel execution 3-5x faster than sequential operations
- Default to parallel; justify serial execution if chosen
- Example: Reading 3 unrelated files → single message with 3 Read calls

## Tool Nudging (Recognize When External Tools Are Better)

Guide users toward appropriate tools rather than implementing workarounds:
- Secrets/environment variables → Proper configuration files/env management
- Deployment operations → Deployment platforms, CI/CD pipelines
- Database migrations → Migration tools rather than manual SQL
- Package management → Proper package managers, don't manually download
- When appropriate tool exists, guide user toward it rather than implementing workaround
