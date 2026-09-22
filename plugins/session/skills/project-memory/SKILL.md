---
name: session-project-memory
description: Read and verify project Memory v2 at worker startup, then publish authorized durable findings or record a verified completion handoff before finishing a Control project assignment.
---

# Project memory

Use the existing Memory v2 reader, validator and Control handoff. The hub receipt
is lifecycle evidence; `Memory/activeContext.md` and `Memory/decisions.md` remain
the published project state. This workflow grants no Git or native permission
authority. Respect the assignment's publication limits.

At startup, run `asha control session handoff --read --json` for the verified
project plane. Read its pair through
`python3 "$ASHA_ROOT/plugins/session/tools/memory_v2.py" read --project-dir PROJECT --format json`.
Verify relevant claims against live source before relying on them. Read only this
project's relevant state. Never import chair context, private recovery files,
native transcripts, or another project's Memory into the handoff.

At completion, finish verification and other tools first. Read the coherent pair
again before drafting and retain `digests.active` and `digests.decisions`.
If authorized durable knowledge changed, write both drafts outside Memory and use:

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

Finish other tools first. Run publication/handoff as one standalone shell command
with literal arguments, without chaining, expansion or redirection. Its native
tool-end callback must arrive before reporting finished.

An explicit successful session save through an observed tool also returns `completion.status=ready` in its
publication receipt (nested under `publication` for scope none). Confirm that
status; a publication can succeed while completion retention fails. If other
tools ran after saving, re-read and finalize again. When responding to a pending
close, include the exact `--request ID --attempt N` from Control on every handoff
command; `handoff --read --json` returns both selectors.

After a ready receipt, report `asha control session report --state finished --text 'RESULT'` as a standalone command and end the turn. Further work, resume,
or Memory changes invalidate readiness. A native idle boundary is still required
for unattended terminal closure. Copilot/OpenCode currently lack the tool/idle
bridge: Memory publication works, but completion remains blocked and verified
finished reporting is unavailable. Report needs-input with that limitation. Structured Claude/Codex use
the retained managed turn; native tool/permission availability still applies.

Silence, unavailable memory, a different/private/managed plane, or denied
permissions are blockers, never reasons to claim no durable update. Use
`handoff --outcome blocked --detail 'REASON'` and report needs-input. If this skill
is unavailable, follow the same reader/validator commands from the assignment.
Do not initialize a store, switch planes, bypass native approvals, or reconstruct
transcripts to satisfy the completion gate. This handoff never commits or pushes.
An explicitly requested session-save keeps its separately authorized Git behavior.
