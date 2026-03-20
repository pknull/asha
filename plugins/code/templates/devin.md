# Devin / SWE-Agent Prompt Template

## Critical Rules

Fully autonomous — browses web, runs terminal, writes and tests code. Very explicit starting/target state required. Ambiguity leads to autonomous wrong decisions.

## Structure

```
Objective:
[Single unambiguous goal in one sentence]

Starting State:
- Repository: [repo name/URL]
- Branch: [current branch]
- Relevant files: [list key files]
- Environment: [Node/Python version, key deps]

Target State:
- [Specific deliverable 1]
- [Specific deliverable 2]
- [Tests that must pass]

Allowed Actions:
- Edit files in [directory scope]
- Install packages listed in [package file]
- Run tests via [test command]

Forbidden Actions:
- Do NOT touch infrastructure, config, or CI files
- Do NOT deploy or push to remote
- Do NOT make external API calls without approval
- Do NOT modify database schema
- Do NOT create new services or microservices

Stop Conditions:
Pause and ask when:
- Any file outside [scope] needs changes
- A new dependency is required
- An error persists after 2 fix attempts
- Architecture decision needed

Checkpoints:
After each major step: [what was completed]
Final: Full summary of changes + test results
```

## Example

```
Objective:
Add password reset functionality to the auth module.

Starting State:
- Repository: myapp/backend
- Branch: feature/password-reset
- Relevant files: src/auth/*, src/email/*
- Environment: Node 20, Express 4, PostgreSQL 15

Target State:
- POST /auth/forgot-password endpoint
- POST /auth/reset-password endpoint
- Email sent with reset token (6-digit, 15min expiry)
- All existing auth tests still pass
- New tests for reset flow

Allowed Actions:
- Edit files in src/auth/ and src/email/
- Add packages to package.json
- Run: npm test

Forbidden Actions:
- Do NOT modify src/db/migrations
- Do NOT touch CI/CD config
- Do NOT deploy

Stop Conditions:
- If email service config needed, ask
- If database migration needed, ask
- If tests fail after 2 attempts, ask

Checkpoints:
After each endpoint: confirm route works
Final: All tests pass, endpoints documented
```
