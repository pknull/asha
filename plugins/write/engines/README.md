# `rp-draft-loop` engine

A generic, profile-driven prose-drafting loop used as a Workflow script. Despite the historical
name, it is **mode-agnostic** — it carries no project-specific paths. All wiring arrives at runtime
via `args.profileConfig` (a resolved *mode manifest*).

> Origin: relocated here from the AAS project (`.claude/workflows/rp-draft-loop.js`) during the
> storytelling-convergence work (Phase 3a). Projects consume it by symlinking their workflow file
> back to this path (same pattern as the `write` agents).

## What it does

```
draft (Prose agent)
  ├─ mode:"solo"  → one agent drafts + self-audits against the profile, returns. (cheap default)
  └─ mode:"gate"  → Prose ─▶ (Critic ‖ Continuity ‖ Director?) score in parallel ─▶ revise ─▶ re-score
                    cap at maxIterations (default 3). (scrutiny tier)
                    Director runs only when profileConfig.directorRubric is set — a pacing /
                    anti-rush reviewer; absent = not run (zero cost). All reviewers must PASS to converge.
```

Output stays in the caller's chat. The engine **never writes to a manuscript or canon file**.

## Inputs (`args`)

| arg | required | meaning |
|-----|----------|---------|
| `profileConfig` | **yes** | a resolved flat mode manifest (see contract below) |
| `beatBrief` (or `brief`) | no | what to draft; defaults to "continue from end-state of context" |
| `mode` | no | `"solo"` \| `"gate"`; default = `profileConfig.defaultRunMode` \|\| `"gate"` |
| `contextFile` | no | overrides `profileConfig.context` |
| `maxIterations` | no | gate-loop cap; default 3 |
| `reviewerModel` | no | gate reviewers' model; default `profileConfig.reviewerModel` \|\| `"sonnet"` |
| `draftModel` | no | drafter model; default `profileConfig.draftModel` \|\| inherit session model |

## `profileConfig` contract (flat)

```js
{
  mode,                 // string key for telemetry/labels (e.g. "rp", "hush")
  label,                // human display
  unit,                 // what one draft produces ("GM-voice RP scene beat", "prose passage")
  rubric,               // ABS path: profile-specific craft rubric (auto-fails + scoring)
  voiceSpec,            // ABS path: voice authority
  craftCore,            // ABS path: SHARED craft-core (universal auto-fails + pacing/anti-rush) — same across modes
                        //   conventionally ${asha}/craft/craft-core-universal.md (ships with this plugin)
  directorRubric,       // OPTIONAL ABS path: enables the Director (pacing/anti-rush) reviewer in gate mode
                        //   conventionally ${asha}/craft/director-rubric.md (ships with this plugin)
  continuityAuthority,  // ABS path: continuity/state authority
  bible,                // ABS path(s): character/world canon (string; multiple joined ' + ')
  context,              // ABS path: starting scene/state file
  defaultRunMode,       // optional: "solo" | "gate"
  reviewerModel,        // optional
  draftModel,           // optional
}
```

All paths must be **absolute** (the engine has no filesystem access and does no substitution).

## How a project consumes the engine

1. **Symlink** the project's workflow to this file so the Workflow registry discovers it:
   ```
   .claude/workflows/rp-draft-loop.js  ->  <ASHA_ROOT>/plugins/write/engines/rp-draft-loop.js
   ```
2. **Author mode manifests** under `.claude/modes/<mode>.yaml` (nested, human-readable — see the
   AAS `mode-manifest-schema.md`). Each manifest declares `roots`, `slots`, `models`, `extensions`.
3. **Resolve** a manifest into the flat `profileConfig` before invoking (the caller does this —
   the engine cannot read files). The mapping:

   | manifest field | → profileConfig key |
   |----------------|---------------------|
   | `mode`, `label`, `unit`, `defaultRunMode` | same |
   | `slots.craftRubric` | `rubric` |
   | `slots.voiceSpec` | `voiceSpec` |
   | `slots.craftCore` | `craftCore` (conventionally `${asha}/craft/craft-core-universal.md`) |
   | `slots.directorRubric` | `directorRubric` (OPTIONAL; conventionally `${asha}/craft/director-rubric.md`) |
   | `slots.continuityAuthority` | `continuityAuthority` |
   | `slots.bible` (string or list) | `bible` (list → `'"a" + "b"'`) |
   | `slots.context` | `context` |
   | `models.reviewer` | `reviewerModel` |
   | `models.draft` | `draftModel` |
   | (every `${mem}`/`${vault}`/`${asha}` token) | substituted from `roots` to an absolute path |

   `extensions.*` (the live-interactive layer) is **not** consumed by this engine.
4. **Invoke**:
   ```
   Workflow({ name: 'rp-draft-loop',
              args: { profileConfig: <resolved>, beatBrief: '…', mode: 'gate' } })
   ```

## Shared craft layer (ships with this plugin)

`plugins/write/craft/` holds the **generic, portable** craft files, fed to every profile:

- `craft-core-universal.md` — universal CRITIC auto-fails (tension/resolution + the **pacing / anti-rush
  family**: `telegraphed_destination`, `arrived_not_approached`, `rushed_increment`, `dwell_deficit`),
  shared craft rules, and the generative directives (incl. *pacing-intent-first / approach-don't-arrive*).
  Fed to every profile's Prose/solo drafter (via `SOURCES`) and to the Critic + solo self-audit. **Not** fed
  to Continuity. A profile rubric adds domain-specific detection **on top**.
- `director-rubric.md` — the optional **Director**'s pacing scoring (enabled per-manifest via
  `slots.directorRubric`).

Projects inherit the whole layer by adding an `asha:` root to the manifest's `roots`
(`asha: <ASHA_ROOT>/plugins/write`) and pointing `slots.craftCore` / `slots.directorRubric` at
`${asha}/craft/...`. A new project gets the universal craft + Director for free; it supplies only its
own profile rubric, voice, and bible.

## Return value

`solo`: `{ beat, profile, mode:"solo", selfCaught[] }`
`gate`: `{ beat, profile, converged, rounds, finalCriticPass, finalContinuityPass, finalDirectorPass?, caughtAndFixed[], unresolved[], critic, continuity, director? }` — `finalDirectorPass`/`director` present only when the Director ran (`directorRubric` set).
On no-output: `{ beat:null, …, error|unresolved }`.

## Notes

- Pure JS run by the Workflow tool: no `fs`, no imports, no `Date.now()`/`Math.random()`.
- The TS language server flags `converged`/`uniq` as "unused" — false positives from the script's
  top-level `return`; both are used in the final return block. `node --check` passes.
