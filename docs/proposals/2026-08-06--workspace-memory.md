---
title: Workspace-aware multi-repository memory
type: proposal
status: draft — pending Keeper ratification
date: 2026-08-06
origin: issues #23–#26 (workspace RFC cluster); scope decisions ruled by Keeper 2026-08-06
---

# Workspace-aware multi-repository memory — design spec

Distilled from the #23–#26 RFC cluster after the 2026-08-06 issue-loop run
rejected all four as undispatchable design work. This document pins the
decisions those rejections named as open, so the work can be decomposed into
small, individually decided increments. The four issues stay open as epics;
build increments get their own rubric-passing issues (see Delivery).

## Ratified decisions (Keeper, 2026-08-06)

1. **v1 scope = core contract only** — manifest, detection + validation,
   `asha workspace status`, doctor reporting, and explicit save scopes
   (#23's delivery step 1). Read-side context injection, the canonical
   knowledge plane, bootstrap (#24), worktrees (#25), and work-items (#26)
   are deferred increments, each gated on the one before it.
2. **Promotion is configurable, PR by default** — `promotion_mode` supports
   `pull-request` (default) and `direct-commit` for solo workspaces.
   Auto-promotion from transcripts stays forbidden in every mode; a high
   confidence score is promotion *evidence*, never promotion *authority*.
3. **Three-harness parity is a v1 ship gate** — workspace detection and
   save-scope isolation must behave identically on Claude Code, Codex, and
   Copilot before v1 ships. Verdicts live in `docs/harness-enforcement.md`
   (the single source of truth), probed live per the existing discipline.
4. **Issue hygiene** — #23–#26 remain open as design epics linking here.
   Each increment is filed as a new small issue: pinned acceptance criteria,
   a named test surface, body under ~4,000 characters (the issue-loop triage
   gate truncates beyond that), one decision per issue.

## Goal

Let a session launched from a parent directory containing several child git
repositories know which workspace it is in, which child repo is active, and
where each kind of memory may be written and committed — without changing
behavior at all for single-repository projects.

## The workspace contract (v1)

### Manifest

`.asha/workspace.json` at the workspace root. The full schema is pinned now
so manifests do not churn as later increments land; v1 *acts* only on the
fields it needs and validates the rest as reserved.

```json
{
  "version": 1,
  "workspace_name": "example-workspace",
  "memory": {
    "operational_root": "Memory",
    "personal_root": "memory-local",
    "shared_root": "knowledge",
    "shared_git_root": ".",
    "promotion_mode": "pull-request"
  },
  "repositories": [
    { "path": "frontend", "role": "web", "docs": "knowledge/repos/frontend" },
    { "path": "service",  "role": "api", "docs": "knowledge/repos/service" }
  ]
}
```

- `promotion_mode`: `"pull-request"` (default) | `"direct-commit"`.
- `repositories[].path` and all memory roots are workspace-relative.
- Unknown fields are preserved, not stripped (forward compatibility).

### Validation (fail closed, no partial writes)

- Resolve every path canonically; reject traversal and anything outside the
  workspace root.
- Every declared repository must be a git worktree; missing ones are
  *reported*, never assumed.
- `shared_git_root` must resolve to a git worktree before any workspace
  memory commit is attempted.
- An invalid manifest is a hard, clearly worded error — not a silent
  fallback to single-repo mode (a swallowed manifest error would be the
  silent-empty-result failure again).

### Detection

Walk upward from the current directory for `.asha/workspace.json` (bounded,
stopping at `$HOME` or filesystem root). **The invariant that outranks the
feature: with no manifest present, behavior is byte-identical to today.**
The first v1 tests to land are the ones pinning that invariant.

### Save scopes

```text
asha session save --scope repo        # stage only the active child repo's operational memory
asha session save --scope workspace   # stage only workspace memory, in shared_git_root
asha session save --scope none        # synthesize only; no staging, commit, or push
```

- A repo save may not stage workspace files; a workspace save may not stage
  child-repo source changes. Staging isolation is test-pinned in both
  directions.
- All existing save machinery is preserved unchanged: transcript identity
  validation, silence markers, orphan recovery, save preflight, the
  hash-bound commit gate, and event archival.

### Status and doctor

`asha workspace status` (human + `--json`) reports: resolved workspace root,
manifest validity, active child repository, memory roots, `shared_git_root`
state, and per-repo presence/branch/dirty state. `asha doctor` gains the
same as a section. JSON output is stable enough for automation and
distinguishes warnings from errors.

## Memory planes (taxonomy pinned now; only starred planes exist in v1)

| Plane | Scope | Writer | v1 |
|---|---|---|---|
| Session state | current session | hooks/runtime | ★ (unchanged) |
| Evaluated local memory | user (`~/.asha/learnings/`) | synthesis + confirm/contradict/retire | ★ (unchanged) |
| Operational workspace memory | workspace | explicit save (`--scope workspace`) | ★ new |
| Canonical workspace knowledge | workspace/team | explicit promotion, reviewed | deferred (v3) |

## Cross-harness parity (v1 ship gate)

The core contract deliberately lives where all three harnesses already meet:
shared Python/bash under `plugins/session/tools/`, the `bin/asha` dispatcher,
and the project-root detection helpers the hook handlers share. Parity means:

- manifest detection and validation run identically from any harness's
  session (same shared code path, no harness-conditional logic);
- save-scope staging isolation is proven by live probe on each harness, the
  same way Copilot lifecycle and Codex PostToolUse were probed;
- each verdict is recorded in `docs/harness-enforcement.md` before v1 is
  called done. Anything that cannot reach parity forces a scope cut or an
  honest documented limitation — not a Claude-only ship.

## Security posture (attended-only surfaces)

Path canonicalization, traversal rejection, staging isolation, and anything
touching where commits/pushes land are guardrail-grade code: built attended,
tests first, never dispatched to the issue-loop. This applies to every
increment, not just v1. Credential handling (remote-URL redaction in #24,
env-file copying in #25, adapter secrets in #26) is likewise excluded from
unattended work permanently.

## Deferred increments (each gated on the previous)

| Increment | Content | Epic |
|---|---|---|
| v2 read-side | session-start workspace detection, bounded knowledge-index injection, source-aware retrieval ranking | #23 |
| v3 knowledge plane | canonical `knowledge/` layout, `asha workspace promote` (configurable mode, PR default), `asha workspace lint` | #23 |
| v4 bootstrap | `asha workspace init/status/doctor --fix`, discovery, instruction adapters, ownership hashes | #24 |
| v5 worktrees | `asha workspace worktree create/status/remove` initiative containers | #25 |
| v6 work-items | local registry, capture/scrub, provider adapter contract | #26 |

Deferred means deferred: nothing in v1 may quietly grow a v2+ surface.

## Delivery

v1 is attended work throughout — dispatcher verbs, save pipeline, and
security-adjacent validation are all outside the issue-loop's permitted
blast radius by its own rubric. The realistic first issue-loop candidates
appear from v2 onward (lint rules, JSON schema additions, doc checks).

First issues to file once this proposal is ratified (each under 4k chars,
each with its named test surface):

1. **Manifest parse + validate** — pure function, typed errors, table-driven
   tests (`tests/python/test_workspace_manifest.py`). Attended (path
   validation is security surface).
2. **Detection walk + byte-identical fallback** — tests extend the existing
   single-root pins (`test_save_preflight.py`, `tests/test-hooks.sh`).
3. **`asha workspace status`** — dispatcher verb + `--json`; suite added to
   `tests/run-tests.sh`.
4. **Save-scope routing + staging isolation** — extends the save pipeline
   tests; both isolation directions pinned.
5. **Three-harness parity probes** — live probes per harness; verdicts
   written to `docs/harness-enforcement.md`.

## Non-goals (carried from the epics, binding on every increment)

- No hard-coded organization, repository inventory, ticket system, reviewer
  identity, or technology stack (the v2.4.0 de-personalization rule applies
  to this subsystem from birth).
- No `knowledge/` requirement for single-repository users; no change of any
  kind for projects without a manifest.
- No replacement of per-user learnings, project `Memory/`, or native harness
  memory; no transcript content promoted to canonical knowledge, ever.
- No implicit `git init`, no implicit branch/worktree/PR creation, and no
  reliance on runtime policy hooks as a substitute for git review and CI.

## Open questions (small, none block ratification)

1. Should the workspace manifest be committed in the workspace's
   `shared_git_root` by convention? (asha's own repos gitignore `.asha/`
   wholesale; target workspaces likely want the manifest versioned —
   probably a documented convention plus a doctor warning, decided in v1.)
2. Does `--scope repo` need a way to name a child repo explicitly when the
   session cwd is the workspace root, or is "active repo = cwd" sufficient
   for v1? (Lean: cwd-only for v1; revisit with v2 read-side.)
