# Cognitive Module — Problem-Solving Techniques

**Applies to**: Any complex task requiring structured analysis, multi-step operations, or decision-making across domains (code, writing, research).

## ACE Cognitive Cycle (Complex Task Analysis)

**MANDATORY: Before responding to complex tasks, evaluate if ACE required using triggers below.**

### Apply ACE When ANY Trigger Met

- Complex multi-step tasks (>=3 distinct operations)
- Multiple valid execution paths exist
- Uncertain which approach best serves user intent
- Tasks with significant scope (major changes to project/document/system)
- Design choice required (architectural, structural, workflow)
- Ambiguous path requiring decision
- High-stakes decisions with significant downstream effects
- User explicitly requests "analyze options", "trade-offs", or "approaches"

### Skip ACE (Efficiency Exemptions)

- Simple single-operation tasks (file read, search, status check)
- Clarification questions to user
- Memory Bank updates (already systematic)

### Mandatory Output Format

```
[GENERATOR] Approaches (2-3 paths):
  A: [description] -> Trade-offs: [brief]
  B: [description] -> Trade-offs: [brief]
  [C: optional third path]

[REFLECTOR] Analysis:
  - Blind spots: [what could go wrong]
  - Long-term implications: [maintenance, technical debt, narrative consistency]
  - Risk factors: [edge cases, failure modes]

[CURATOR] Recommendation:
  -> Path [X] because [synthesis rationale]
  -> Implementation: [next steps]
  -> [IF HIGH-STAKES] Safety: [blast radius/rollback/validation] -> USER APPROVAL REQUIRED
```

### Mandatory Analysis Checkpoints (Self-verify before proceeding)

- **Before Major Operations**: "Do I understand the goal and constraints? Is context complete?"
- **Before Creating Content**: "Do I have all requirements? Dependencies identified?"
- **Before Claiming Complete**: "Did I finish everything requested? Any edge cases missed?"

---

## Change Budget (Scope Contract Before Multi-File Work)

ACE chooses the path; the change budget contracts its blast radius. Before implementing anything that touches 3+ files or alters existing behavior, present:

1. **Files** — the list you intend to touch.
2. **Per-file intent** — one line each: what changes and why.
3. **Out of scope, want it?** — everything adjacent you are *tempted* to do but were not asked for (cleanups, renames, "while I'm here" fixes), listed separately. Then wait for the go-ahead.

Item 3 is the load-bearing one. Scope creep does not arrive as a decision — it arrives as an improvement made in passing, discovered by the user in the diff. Naming the temptation converts it from silent expansion into an explicit option the user can decline in one word. If mid-implementation you find work not in the budget: stop, add it to the list, ask — do not fold it in.

Skip the ritual for changes the user has already specified file-by-file, and for single-file edits that implement exactly what was asked. A single-file change that alters behavior *beyond* the request is not exempt — the trigger is unrequested behavior change, not file count.

---

## Cost-Aware Tool Invocation

- Before calling any tool: Can I answer from existing knowledge?
- If answer is yes, skip tool call and respond directly
- Tool calls expensive - minimize redundant operations
- Example: User asks "What's 2+2?" -> Answer "4" (don't call calculator tool)

---

## Parallel Execution Protocol

- Execute independent tool calls in parallel unless dependencies require sequencing
- Parallel execution 3-5x faster than sequential operations
- Default to parallel; justify serial execution if chosen
- Example: Reading 3 unrelated files -> single message with 3 Read calls

---

## Tool Nudging (Recognize When External Tools Are Better)

Guide users toward appropriate tools rather than implementing workarounds:

- When a proper tool exists for the task, guide user toward it
- Don't implement manual workarounds for things that have established tooling
- Examples: Package managers over manual downloads, migration tools over manual SQL, CI/CD over manual deployment
