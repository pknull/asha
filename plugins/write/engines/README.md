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
  └─ mode:"gate"  → Prose ─▶ (Critic ‖ Continuity) score in parallel ─▶ revise ─▶ re-score
                    cap at maxIterations (default 3). (scrutiny tier)
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
  craftCore,            // ABS path: SHARED craft-core (universal auto-fails) — same across modes
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
   | `slots.craftCore` | `craftCore` |
   | `slots.continuityAuthority` | `continuityAuthority` |
   | `slots.bible` (string or list) | `bible` (list → `'"a" + "b"'`) |
   | `slots.context` | `context` |
   | `models.reviewer` | `reviewerModel` |
   | `models.draft` | `draftModel` |
   | (every `${mem}`/`${vault}` token) | substituted from `roots` to an absolute path |

   `extensions.*` (the live-interactive layer) is **not** consumed by this engine.
4. **Invoke**:
   ```
   Workflow({ name: 'rp-draft-loop',
              args: { profileConfig: <resolved>, beatBrief: '…', mode: 'gate' } })
   ```

## Return value

`solo`: `{ beat, profile, mode:"solo", selfCaught[] }`
`gate`: `{ beat, profile, converged, rounds, finalCriticPass, finalContinuityPass, caughtAndFixed[], unresolved[], critic, continuity }`
On no-output: `{ beat:null, …, error|unresolved }`.

## Notes

- Pure JS run by the Workflow tool: no `fs`, no imports, no `Date.now()`/`Math.random()`.
- The TS language server flags `converged`/`uniq` as "unused" — false positives from the script's
  top-level `return`; both are used in the final return block. `node --check` passes.
