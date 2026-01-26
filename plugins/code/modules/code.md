# Code Module — Software Development

**Applies to**: Software development tasks involving code writing, refactoring, debugging, or technical implementation.

**Prerequisite**: For complex tasks, apply ACE Cognitive Cycle from `asha/modules/cognitive.md` first.

---

## Convention Matching Protocol (Before Writing Code)

- Check Memory/techEnvironment.md for documented code conventions
- If conventions exist, follow them (naming, libraries, patterns, style)
- If conventions unclear, read example files to understand patterns
- Update Memory/techEnvironment.md with discovered conventions for future sessions
- Verify library availability in codebase before using (don't assume dependencies exist)

---

## Code Comments

- Default to minimal or no comments - prefer self-documenting code
- Only add comments when logic is non-obvious or complex
- Good variable/function names better than explanatory comments
- Example: `getUserById(id)` not `getUser(id) // gets user by ID`

**Change Comments** (non-obvious edits only):
- Add inline justification: `// <CHANGE> brief explanation`
- Helps code review and clarifies reasoning behind subtle fixes

---

## Code References Format

When referencing specific code locations: `file_path:start_line:end_line`

Example: "Bug located in /home/user/project/src/api/handler.ts:42:58"

Enables direct navigation to exact location.

---

## High-Stakes Code Operations

Production deployments, breaking changes, migrations, security-sensitive code:
- Document blast radius (affected files/systems/users)
- Define rollback procedure (reversal steps)
- Specify validation method (success/failure confirmation)
- Require explicit user approval before execution

Cross-reference: `asha/modules/high-stakes.md` for full safety protocol.
