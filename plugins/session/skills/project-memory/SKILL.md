---
name: session-project-memory
description: Read and verify project Memory v2, then publish authorized durable findings or attest no durable update. Use at worker startup and before finishing a Control project assignment.
---

# Project memory

Use the existing Memory v2 reader, validator and Control handoff. Control records
each successful save as a publication row, which is how a session reads as
saved; `Memory/activeContext.md` and `Memory/decisions.md` remain the published
project state. This workflow grants no Git or native permission authority.
Respect the assignment's publication limits.

Run each `asha control session` command as its own tool call, on its own with
literal arguments: no `&&`, pipes, redirection, `$VAR` or heredoc. Codex runs
only that plain form outside its sandbox, where Control can prove the caller is
this session; chained or expanded, it stays sandboxed and fails with `session
ownership unavailable` or `reporter is not part of this session`. When an error
says to run the command on its own, do that.

At startup, run `asha control session handoff --read --json` for the verified
project plane. Then, as a separate command, read its pair through
`python3 "$ASHA_ROOT/plugins/session/tools/memory_v2.py" read --project-dir PROJECT --format json`.
Verify relevant claims against live source before relying on them. Read only this
project's relevant state. Never import chair context, private recovery files,
native transcripts, or another project's Memory into the handoff.

At completion, finish verification and other tools first. Run
`asha control session handoff --read --json` again and retain its
`memory.baseline` digests for `activeContext.md` and `decisions.md`, then reread
the pair before drafting. Taking the digests first means a save that lands in
between fails the compare-and-swap instead of being overwritten. If authorized
durable knowledge changed, write both drafts outside Memory with your edit tool
and use:

```bash
asha control session handoff --active-file ACTIVE --decisions-file DECISIONS --expected-active DIGEST --expected-decisions DIGEST --json
```

The shared validator enforces the compact Memory format. `activeContext.md` has
exact level-one headings Objective, State, Next, Blockers, at most 4096 UTF-8 bytes
and five Next/Blockers items each. `decisions.md` is headed Decisions and contains
only current binding decisions. A changed baseline requires rereading and merging;
never refresh digests merely to force an old draft through.

When nothing durable changed, attest that explicitly:

```bash
asha control session handoff --outcome no-durable-update --detail 'WHY' --json
```

A successful handoff answers `hub_publication_status: recorded`; an explicit
session save through the publisher returns the same field in its publication
receipt (nested under `publication` for scope none). A publication can succeed
while Control fails to record it (`unavailable`); say so. When responding to a
close request, name it with `--request ID` from the request or `handoff --read
--json`; a save without it still counts.

Then report `asha control session report --state finished --text 'RESULT'` and
end the turn. Finished is never gated: the row reads finished, saved HH:MM or
finished, unsaved. A close asks for this save, waits a bounded time, then
terminates the session. Structured Claude/Codex use the retained managed turn;
native tool/permission availability still applies.

Silence, unavailable memory, a different/private/managed plane, or denied
permissions are blockers, never reasons to claim no durable update. Use
`handoff --outcome blocked --detail 'REASON'` and report needs-input. If this skill
is unavailable, follow the same reader/validator commands from the assignment.
Do not initialize a store, switch planes, bypass native approvals, or reconstruct
transcripts to make a session read as saved. This handoff never commits or pushes.
An explicitly requested session-save keeps its separately authorized Git behavior.
