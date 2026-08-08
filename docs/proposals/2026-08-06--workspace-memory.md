---
title: Workspace-aware multi-repository memory
type: proposal
status: delivered — ratified by Keeper via merge of PR #28 (2026-08-07 UTC); all delivery issues shipped (#31/#33/#29/#35/#36/#39 for v1; epics #23–#26 closed by PR #51, 2026-08-08 UTC)
date: 2026-08-06
origin: issues #23–#26 (workspace RFC cluster); scope decisions ruled by Keeper 2026-08-06; amended per panel review and codex second-pass 2026-08-07 UTC (Work/panels/2026-08-06--pr28-workspace-proposal-review/); capabilities-schema ruling amended under issue #39 2026-08-08 UTC (see Status and doctor)
---

# Workspace-aware multi-repository memory — design spec

Distilled from the #23–#26 RFC cluster after the 2026-08-06 issue-loop run
rejected all four as undispatchable design work. This document pins the
decisions those rejections named as open, so the work can be decomposed into
small, individually decided increments. The four issues stay open as epics;
build increments get their own issues (see Delivery).

**Precedence**: on pinned decisions and the manifest schema, this document
outranks the epics' prose. Changes land here first; the epics link here.
**Ratification mechanics**: merging PR #28 constitutes ratification, and the
merge flips the frontmatter `status` to `ratified`.

## Ratified decisions (Keeper, 2026-08-06)

1. **v1 scope = core contract only** — manifest, detection + validation,
   `asha workspace status`, doctor reporting, and explicit save scopes
   (#23's delivery step 1). Read-side context injection, the canonical
   knowledge plane, bootstrap (#24), worktrees (#25), and work-items (#26)
   are deferred increments (see Deferred increments for sequencing).
2. **Promotion is configurable, PR by default** — `promotion_mode` supports
   `pull-request` (default) and `direct-commit` for solo workspaces.
   Auto-promotion from transcripts stays forbidden in every mode; a high
   confidence score is promotion *evidence*, never promotion *authority*.
3. **Three-harness parity is a v1 ship gate** — workspace detection and
   save-scope isolation must be proven on Claude Code, Codex, and Copilot
   before v1 ships, on every save path that exists on that harness (see
   Cross-harness parity for the per-harness reality). Verdicts live in
   `docs/harness-enforcement.md` (the single source of truth), probed live
   per the existing discipline.
4. **Issue hygiene** — #23–#26 remain open as design epics linking here.
   Each increment is filed as a new small issue written to *loop-grade
   hygiene*: pinned acceptance criteria, a named test surface, one decision
   per issue, body under ~4,000 characters (the issue-loop's fetch stage
   truncates longer bodies before triage ever reads them). Loop-grade
   hygiene is an issue-quality bar, not a dispatch decision — v1's issues
   meet it and are still never dispatched to the loop (see Security
   posture).

## Goal

Let a session launched from a parent directory containing several child git
repositories know which workspace it is in, which child repo is active, and
where each kind of memory may be written and committed — without changing
behavior at all for single-repository projects.

## The workspace contract (v1)

### Manifest

`.asha/workspace.json` at the workspace root. The full schema is pinned now
so manifests do not churn as later increments land. v1 *acts* only on the
fields it needs; pinned-but-inert fields (e.g. `promotion_mode`,
`shared_root`) are shape-checked as reserved, and truly unknown keys pass
through untouched (forward compatibility — preserved, never stripped).

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

### Root → plane → commit-repo mapping (pinned)

- **Operational workspace memory** (v1's new plane) lives under
  `memory.operational_root`. A workspace save stages only files under that
  root, **commits** in `shared_git_root`, and then follows the same durable
  push path as today's project saves — `push_retry.py ensure` after the
  commit, queue drain on explicit saves, `--no-push` honored. (An earlier
  draft claimed saves never push; that was false to the pipeline.)
- `memory.personal_root` is **reserved** in v1: shape- and
  containment-validated, never staged by any save scope — enforced by the
  save helper's path filter, *not* by `.gitignore`, which cannot protect
  already-tracked files — and carrying no other v1 semantics. Its plane (a
  workspace-local private store) arrives only via a future proposal.
- `memory.shared_root` (canonical knowledge) is inert until v3.

### Validation (fail closed, no partial writes)

- Resolve every path canonically; reject traversal and anything outside the
  workspace root.
- **Containment**: `operational_root` must resolve *inside* the
  `shared_git_root` worktree — the write root and the commit repo cannot
  diverge. A manifest that places it elsewhere fails validation.
- **Disjointness**: the three memory roots must be pairwise disjoint — none
  may nest inside another (`personal_root: "Memory/private"` must fail, or
  a workspace `git add` of the operational root would stage private files).
- **v1 value pin**: `operational_root` must equal `"Memory"` (the schema
  default) in v1. The existing save preflight, hash-bound commit gate, and
  event machinery all key on the literal `Memory/` path
  (`save_preflight.py`, `save-commit-gate.sh`); an arbitrary root would
  bypass them silently. Other values fail validation as "reserved for a
  future increment" — configurability is deferred, not dropped.
- Every declared repository must be a git worktree; missing ones are
  *reported*, never assumed.
- `shared_git_root` must resolve to a git worktree before any workspace
  memory commit is attempted.
- An invalid manifest is a hard, clearly worded error — not a silent
  fallback to single-repo mode (a swallowed manifest error would be the
  silent-empty-result failure again).

### Detection

Walk upward from the current directory for `.asha/workspace.json`, stopping
**before `$HOME`** and **before the filesystem root** — both exclusive:
neither `$HOME` nor `/` can ever be a workspace root (a root-level manifest
would make every session on the machine part of one global workspace).
Ancestor-vs-`$HOME` comparisons use canonicalized paths, so a symlinked home
entered via its physical path still stops in the same place.
`~/.asha/workspace.json` is additionally reserved-invalid: the user-scope
config directory (which exists for every asha install) can never make the
home directory a workspace. **The invariant that outranks the feature: with no
manifest present, behavior is byte-identical to today.** The first v1 tests
to land are the ones pinning that invariant.

### Save scopes

```text
/session:save --scope repo        # stage only the active child repo's operational memory
/session:save --scope workspace   # stage only operational workspace memory; commit in shared_git_root
/session:save --scope none        # synthesize only; no staging, commit, or push
```

(Syntax shown as the Claude slash command; on Codex and Copilot the same
surface is the installer-rendered `session-save` command-skill. A `bin/asha`
CLI verb is explicitly **out of v1 scope** — no documented invocation may
depend on a verb no delivery issue builds.)

- **Defaults, pinned**: inside a workspace, a bare save behaves as
  `--scope repo`. Without a manifest, a bare save is byte-identical to
  today, and passing any `--scope` flag is a hard error. `--scope` thereby
  becomes *reserved argument syntax*: today the same text would be swallowed
  as free-form commit-message words, so rejecting it is the **single
  intentional deviation** from the byte-identical invariant, carved out
  here. `--scope repo` resolves the active child from cwd; run from the
  workspace root itself it fails with guidance (use `--scope workspace`, or
  cd into a child) — explicit child naming is deferred to v2.
- A repo save may not stage workspace files; a workspace save may not stage
  child-repo source changes. Staging isolation is test-pinned in both
  directions.
- All existing save machinery is preserved unchanged: transcript identity
  validation, silence markers, orphan recovery, save preflight, the
  hash-bound commit gate, and event archival.

### Mechanism (where scope routing actually lives)

Saving is **not** a `bin/asha` verb today. There are two independent,
hardcoded commit sites: the interactive `/session:save` path (inline git in
`plugins/session/commands/save.md`) and the automatic SessionEnd path
(`plugins/session/tools/save-session.sh` via `detached-save.sh`). v1's scope
routing therefore lands in the save pipeline itself: the two commit sites
consolidate into one shared, scope-aware helper, reached through the
`session-save` command surface each harness already renders. A `bin/asha`
CLI verb is explicitly out of v1 scope (see Save scopes) — v1 documents no
invocation that no delivery issue builds.

Likewise, project-root detection is currently fragmented across **at least
four** divergent forms: `detect_project_dir` in two copies
(`hooks/handlers/common.sh`, `tools/save-session.sh`), an ad-hoc pattern in
two handlers (`save-commit-gate.sh`, `save-preflight-stop.sh`), and
`resolve_project_dir` in `tools/save-preflight-env.sh` — plus Python-side
detectors. That inventory is illustrative, not exhaustive: first issue 2's
acceptance criterion is an **audit**, not a checklist — after consolidation,
a repo-wide search must find no remaining independent project-root fallback
chain, because divergent chains cannot all stay "byte-identical".

### Status and doctor

`asha workspace status` (human + `--json`) reports: resolved workspace root,
manifest validity, active child repository, memory roots, `shared_git_root`
state, and per-repo presence/branch/dirty state. `asha doctor` gains the
same as a section. JSON output is stable enough for automation and
distinguishes warnings from errors.

Carried from #23's harness contract: `harnesses/capabilities.json` gains a
`workspace` capability entry per harness reporting detection, save-scope
isolation, and automatic-save availability — so a user's own `asha doctor`
surfaces harness-specific workspace limitations without reading asha's
internal docs. The schema's open capability *map* permits the new key, but
its capability *value* schema is closed (a single `support` enum plus
`limitations` prose), so per-facet reporting requires a schema extension —
that entry and any extension are owned by first issue 6 (probe-derived
values), not issue 3.

> **Amendment (issue #39, 2026-08-08)**: issue 6 ruled the per-facet schema
> extension **not warranted for v1**. The `workspace` entry ships under the
> closed v3 value schema, with the four facets (detection / save-scope
> isolation / auto-save / gate enforcement) reported as precise
> `limitations` strings; `asha doctor` prints them verbatim in its
> workspace section. Revisiting per-facet fields requires its own schema
> change with its own review — not a rider on an attestation.

## Memory planes (taxonomy pinned now; only starred planes exist in v1)

| Plane | Scope | Writer | v1 |
|---|---|---|---|
| Session state | current session | hooks/runtime | ★ (unchanged) |
| Evaluated local memory | user (`~/.asha/learnings/`) | synthesis + confirm/contradict/retire | ★ (unchanged) |
| Operational workspace memory | workspace | explicit save (`--scope workspace`) | ★ new |
| Canonical workspace knowledge | workspace/team | explicit promotion, reviewed | deferred (v3) |

Issue #23's *optional workspace-local evaluated store* is deliberately cut
from every pinned increment — evaluated memory stays user-scope. If ever
revisited, it re-enters through a new proposal, not through scope drift.

## Cross-harness parity (v1 ship gate)

The core contract deliberately lives in shared code: Python/bash under
`plugins/session/tools/`, the save pipeline, and the consolidated detection
helper — not in harness-specific hooks.

Per-harness reality (per `docs/harness-enforcement.md`): the manual save
surface exists on all three harnesses (native `/session:save` on Claude;
installer-rendered `session-save` command-skills on Codex and Copilot — they
receive generated skills, not literal slash commands); the **automatic**
lifecycle save is wired on Claude Code and Copilot, while Codex has no wired
auto-save path at all — a pre-existing gap unrelated to workspace support.

The gate, scoped accordingly: workspace detection and save-scope staging
isolation are proven by live probe **on every save path that exists on that
harness** — both paths on Claude and Copilot, the manual path on Codex.
Building Codex auto-save wiring is not a v1 prerequisite; its absence is
reported per-harness via the `capabilities.json` workspace entry and
recorded in `docs/harness-enforcement.md` before v1 is called done. A
capability that cannot reach parity on an *existing* path forces a scope cut
— not a Claude-only ship, and not a silently invoked "documented limitation"
escape hatch.

## Security posture (attended-only surfaces)

Path canonicalization, traversal rejection, staging isolation, and anything
touching where commits land are guardrail-grade code: built attended, tests
first, never dispatched to the issue-loop. This applies to every increment,
not just v1. Credential handling (remote-URL redaction in #24, env-file
copying in #25, adapter secrets in #26) is likewise excluded from unattended
work permanently. Because the loop's `no_security_surface` triage criterion
is an LLM self-assessment (hard-enforced once judged, but judged from issue
text alone), these increment issues carry an explicit attended label rather
than relying on triage to recognize them.

**Known policy-rail gap, closed as defense-in-depth**: the `destructive-git`
guard matches only commands where the destructive verb directly follows
`git` — `git -C <dir> push --force …` bypasses it (verified against the live
regex), and no Test 104 pin covers `-C`/`--git-dir`/`--work-tree` forms. To
be precise about the exposure: workspace saves themselves use only add,
commit, and the plain push the guard deliberately allows — the save path
does not exercise the gap. What the feature changes is that agents routinely
running git against a *second* repo root becomes normal, and destructive
protection currently does not reach that form at all. Extending the rule and
pinning it is therefore a v1 first issue (issue 5) that lands **before** any
`shared_git_root` write ships — defense-in-depth for the command class the
feature normalizes, not a prerequisite of the save path itself.

## Deferred increments

| Increment | Content | Epic |
|---|---|---|
| v2 read-side | session-start workspace detection, bounded index injection (operational workspace memory only — `shared_root` stays inert until v3, whose canonical index is the first thing to read it), source-aware retrieval ranking | #23 |
| v3 knowledge plane | canonical `knowledge/` layout, `asha workspace promote` (configurable mode, PR default), `asha workspace lint` | #23 |
| v4 bootstrap | `asha workspace init/status/doctor --fix`, discovery, instruction adapters, ownership hashes | #24 |
| v5 worktrees | `asha workspace worktree create/status/remove` initiative containers | #25 |
| v6 work-items | local registry, capture/scrub, provider adapter contract | #26 |

The ordering is a **sequencing policy** (one increment in flight at a time),
not a dependency claim: v4–v6 depend materially only on v1, plus v3 for
bootstrap's knowledge-root stub. Keeper may reorder without re-ratification.
Deferred still means deferred: nothing in v1 may quietly grow a v2+ surface.

## Delivery

v1 is attended work throughout — save-pipeline changes, dispatcher verbs,
and security-adjacent validation are all outside the issue-loop's permitted
blast radius by its own triage rubric. The realistic first loop candidates
appear from v2 onward (lint rules, JSON schema additions, doc checks).

First issues to file once this proposal is ratified (each to loop-grade
hygiene; attended labels where marked):

1. **Manifest parse + validate** — pure function, typed errors, containment
   checks, table-driven tests (`tests/python/test_workspace_manifest.py`).
   Attended (path validation is security surface).
2. **Detection walk + byte-identical fallback** — consolidates every
   project-root detection chain into one shared helper; acceptance is an
   audit (no independent fallback chain remains repo-wide), and tests extend
   the existing single-root pins (`test_save_preflight.py`,
   `tests/test-hooks.sh`).
3. **`asha workspace status` + doctor section** — dispatcher verb, `--json`,
   and the doctor reporting from v1 scope; suite added to
   `tests/run-tests.sh`. (The `capabilities.json` entry belongs to issue 6,
   which owns probe-derived values.)
4. **Save-scope routing** — consolidate the two commit sites into the shared
   scope-aware helper; staging isolation pinned in both directions; the
   preflight gates, hash-bound commit gate, and push-retry queue must
   function against `shared_git_root` (acceptance criteria name all three).
   Attended.
5. **`destructive-git` cross-repo extension** — `-C`/`--git-dir`/
   `--work-tree` arms + Test 104 pins. Attended, guardrail-grade; lands
   before any `shared_git_root` write ships.
6. **Three-harness parity probes + workspace capability entry** — a
   gate-tracking issue (exempt from the test-surface rule: its deliverables
   are live-probe verdicts written to `docs/harness-enforcement.md` and the
   `capabilities.json` workspace entry, extending the capability value
   schema if per-facet fields are warranted). Last.

Ordering: 1 → 2 → {3, 4} → 5 before any workspace write ships → 6 closes
the gate.

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

## Open questions

1. **RESOLVED (Keeper, 2026-08-08; implemented in v1 issue #35)**: the
   workspace manifest is **committed in `shared_git_root` by convention**.
   `asha workspace status` and `asha doctor` warn when it is untracked
   there; an invalid manifest is a typed error with **guided repair steps**
   (never an auto-fix), which softens the accepted consequence that a
   committed manifest plus fail-closed validation lets one teammate's typo
   hard-fail every session workspace-wide until repaired. (asha's own repos
   continue to gitignore `.asha/` — the convention binds workspaces, not
   this toolkit's repositories.)
