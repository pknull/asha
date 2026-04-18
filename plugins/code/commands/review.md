---
description: "Review code changes with parallel specialized reviewers"
argument-hint: "[path] | --all"
allowed-tools: ["Task", "Bash", "Read", "Grep", "Glob"]
---

# /code:review

Review local code changes with parallel specialized reviewers and validation.

## Usage

```
/code:review              # Review staged changes (git diff --cached)
/code:review <path>       # Review specific file(s)
/code:review --all        # Review all uncommitted changes (git diff)
```

## Execution

### Step 1: Gather Changes

Based on input:
- **No args**: `git diff --cached` (staged changes only)
- **Path provided**: Read the specified file(s)
- **--all flag**: `git diff` (all uncommitted changes)

If no changes found, report and exit.

### Step 2: Parallel Review

Launch 4 Task agents **in parallel** (single message, multiple tool calls), each with a specialized focus:

#### Security Reviewer
```
Review this code for security issues:
- Injection vulnerabilities (SQL, command, XSS)
- Authentication/authorization flaws
- Hardcoded secrets or credentials
- Unsafe deserialization
- Path traversal risks

Code to review:
{diff_content}

List findings with file:line references. If none found, state "No security issues identified."
```

#### Logic Reviewer
```
Review this code for logic errors:
- Incorrect algorithms or calculations
- Wrong conditionals or comparisons
- Off-by-one errors
- Incorrect state management
- Broken control flow

Code to review:
{diff_content}

List findings with file:line references. If none found, state "No logic issues identified."
```

#### Edge Case Reviewer
```
Review this code for edge case handling:
- Null/undefined/empty inputs
- Boundary conditions (0, -1, MAX_INT)
- Empty collections or strings
- Race conditions or concurrency issues
- Error paths and exception handling

Code to review:
{diff_content}

List findings with file:line references. If none found, state "No edge case issues identified."
```

#### Style Reviewer
```
Review this code for style and maintainability:
- Unclear naming or confusing logic
- Code duplication
- Overly complex functions (consider splitting)
- Missing or misleading comments
- Inconsistent patterns with surrounding code

Code to review:
{diff_content}

List findings with file:line references. If none found, state "No style issues identified."
```

### Step 3: Validation Pass

After all reviewers complete, validate each finding:

For each issue found, verify:
1. **Existence**: Does the referenced code actually exist at that location?
2. **Accuracy**: Does the finding correctly describe the issue?
3. **Applicability**: Is this actually a problem in context, or a false positive?

Remove findings that fail validation. Note any that were filtered.

### Step 4: Present Results

Output format:

```markdown
## Code Review Results

**Scope**: {what was reviewed}
**Files**: {count} | **Lines**: {count}

### Security
{findings or "No issues"}

### Logic
{findings or "No issues"}

### Edge Cases
{findings or "No issues"}

### Style
{findings or "No issues"}

---
**Validation**: {N} findings filtered as false positives
```

## Notes

- Uses Task tool with subagent_type appropriate for code review
- Parallel execution minimizes wait time
- Validation pass reduces noise from false positives
- For very large diffs (>1000 lines), recommend splitting the review

<!-- RED-FLAGS:START -->
## Red Flags — Stop and Reconsider

If you catch yourself thinking any of the following while running `/code:review`, stop. The thought itself is the warning. Do the action in the right column instead.

| Rationalization (the thought) | What it actually means | Do this instead |
|---|---|---|
| "Reviewer flagged this but it's probably a false positive — I'll filter it." | The Step 3 validation pass exists to *verify*, not to suppress. Filtering on vibe is how real findings get dropped. | Run the actual existence/accuracy check on the finding. If it survives, surface it — even if you suspect noise. |
| "Diff is huge, I'll spot-check rather than route through the four reviewers." | Large diffs are exactly where reviewers earn their keep. Spot-checking a 1000-line change misses the bug by definition. | Either split the diff (per the >1000-line note) or run all four reviewers across the whole thing. No spot-check shortcut. |
| "All four reviewers said 'no issues' — done, no need for a second pass." | Single-pass review is the failure mode the user's CLAUDE.md "Multi-Pass Review" rule explicitly calls out. | Do an adversarial second pass yourself: assume bugs exist, look for the stupidest interpretation. First-pass clean is the trigger to dig harder, not stop. |
| "Style nits aren't worth surfacing — they'll just annoy the user." | Style nits compound into maintainability debt and the Style reviewer was invoked for a reason. | Surface them, grouped under Style, with severity indication. Let the user decide what to ignore. |
| "I already reviewed this mentally while reading the diff — skip the parallel agents." | You just skipped the entire purpose of the command. Mental review has none of the four specialized lenses. | Run the four parallel reviewers anyway. Your read informs the synthesis, it doesn't replace the agents. |
| "Validation says line numbers don't match — author probably reformatted, I'll keep the finding." | Mismatched line numbers can mean the finding was hallucinated against a stale or imagined version of the file. | Treat line-number mismatch as a strong hallucination signal. Re-verify the finding exists in the actual file before keeping it. |
| "User wants speed — one reviewer is enough this time." | Single-reviewer mode is not a documented option of this command. You're inventing a degraded path the user didn't ask for. | Run all four in parallel — that *is* the speed path. If the user truly wants less, ask explicitly which lens to drop. |

**General rule**: rationalization that *sounds* reasonable in the moment is the strongest signal. Genuine exceptions are rare; rationalized shortcuts are common.
<!-- RED-FLAGS:END -->
