# Local Review

**Version**: 1.0.1
**Command**: `/local-review`

Parallel code review with 4 specialized reviewers plus validation pass to filter false positives.

## Installation

```bash
/plugin marketplace add pknull/asha-marketplace
/plugin install local-review@asha-marketplace
```

## Usage

```bash
/local-review              # Review staged changes (git diff --cached)
/local-review <path>       # Review specific file(s)
/local-review --all        # Review all uncommitted changes (git diff)
```

## Reviewers

The plugin launches 4 parallel review agents, each with a specialized focus:

| Reviewer | Focus Areas |
|----------|-------------|
| **Security** | Injection vulnerabilities, auth flaws, hardcoded secrets, unsafe deserialization, path traversal |
| **Logic** | Incorrect algorithms, wrong conditionals, off-by-one errors, state management, control flow |
| **Edge Cases** | Null/empty inputs, boundary conditions, race conditions, error handling |
| **Style** | Unclear naming, code duplication, complexity, comments, inconsistent patterns |

## Validation Pass

After all reviewers complete, findings are validated to filter false positives:

1. **Existence**: Does the referenced code actually exist?
2. **Accuracy**: Is the finding correctly described?
3. **Applicability**: Is this actually a problem in context?

## Output

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

## License

MIT License
