# /local-review

Review local code changes with parallel specialized reviewers and validation.

## Usage

```
/local-review              # Review staged changes (git diff --cached)
/local-review <path>       # Review specific file(s)
/local-review --all        # Review all uncommitted changes (git diff)
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
## Local Review Results

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
