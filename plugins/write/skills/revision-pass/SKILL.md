---
name: write-revision-pass
description: Run a complete manuscript revision pass from authority-first reading through read-only act reviews, decision capture, style audit, repository-wide old-value proof, and a root commit. Use when a requested revision spans acts or sections and must prove that a superseded value is gone.
---

# Revision Pass

Execute the requested revision without asking clarifying questions. Resolve
ambiguity from the repository's authority hierarchy; record irreducible
judgment calls instead of silently inventing an answer.

## Contract

1. **Read authority before prose.** Identify and read the repository's binding
   project instructions, manuscript plan, canon/state sources, and style or
   voice document before editing any manuscript text. Treat live manuscript
   and authority files as stronger evidence than summaries or review notes.
2. **Define the pass.** State the old value being retired, the intended new
   value when one exists, the acts/sections in scope, and the acceptance proof.
   Resolve `ASHA_ROOT` from the wrapper environment or
   `${ASHA_HOME:-$HOME/.asha}/config.json`, then run
   `"$ASHA_ROOT/plugins/session/tools/declare-pass.sh" OLD [NEW]` when that
   tool is available so the end-of-turn hook can recheck the declaration.
3. **Revise in manuscript order.** Preserve facts and language outside the
   stated pass. Do not broaden into an unrelated rewrite.
4. **Review every act independently.** Read
   `harnesses/capabilities.json`, select the current harness's `subagents`
   surface, and assign exactly one read-only review agent to each act. Agents
   may run in parallel when that surface exists. If no usable subagent surface
   exists, perform the same read-only act reviews sequentially. A reviewer may
   report findings but must not edit manuscript or authority files.
5. **Do not ask clarifying questions.** Resolve mechanical ambiguity from the
   authority sources. Append every genuine creative or continuity judgment
   call to the manuscript project's `DECISIONS.md`, following its existing
   format, before relying on that decision in prose.
6. **Audit the revision against style.** Re-read the authoritative style or
   voice document, then audit your own changed prose against it. Fix confirmed
   deviations; do not substitute generic preferences for project rules.
7. **Prove the old value is gone.** From the repository root, run a scoped
   repository-wide fixed-string search that excludes `.git`, `.jj`, and
   `Work`, for example:

   ```bash
   grep -rInF --exclude-dir=.git --exclude-dir=.jj --exclude-dir=Work \
     -- "OLD" .
   ```

   Record the exact command and its empty output. A non-empty result means the
   pass is incomplete; revise and rerun rather than explaining the residue
   away.
8. **Commit from the repository root.** Run the repository's required checks,
   inspect the bounded diff, and create the requested commit from the root.
   Never commit from a manuscript subdirectory.

## Output

Report the authority files read, act-review verdicts, entries added to
`DECISIONS.md`, style-audit outcome, verification commands with exit status,
the exact empty old-value grep proof, and the resulting commit identity.
