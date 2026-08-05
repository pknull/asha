# `rp-draft-loop` engine

A generic, profile-driven prose-drafting loop used as a Workflow script. Despite the historical
name, it is **mode-agnostic** — it carries no project-specific paths. All wiring arrives at runtime
via `args.profileConfig` (a resolved *mode manifest*).

> Origin: relocated here from a consuming project's `.claude/workflows/rp-draft-loop.js` during the
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
  mode,                 // string key for telemetry/labels (your profile's name)
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

2. **Author mode manifests** under `.claude/modes/<mode>.yaml` (nested, human-readable; a project
   may keep its own `mode-manifest-schema.md` alongside them). Each manifest declares `roots`,
   `slots`, `models`, `extensions`.
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

---

# `commission-loop` engine

The adversarial commissioning harness (added v1.9.0, from the 2026-08-04 usage-insights work): fan-out
drafting where **fabrication is the risk**. N independent workers draft one brief from distinct angles;
every factual claim must carry a source path + verbatim quote; a per-draft verifier panel tries to
**refute** the claims against the sources themselves; a ranker orders only the survivors. Rejects are
returned *with their findings* — silence is never success.

```
Commission (N workers, one angle each)        pipeline: a draft enters verification
  └─▶ Verify (per-draft panel, one agent      the moment it exists — no barrier
       per lens: fabrication ‖ contradiction ‖ …)
        └─▶ Rank (survivors only; barrier — ranking needs all of them)
```

The write boundary is structural: every stage **returns** text; the engine returns a report; nothing
touches a project file. Promotion of a shortlisted artifact is the caller's explicit act — the same
gate discipline as the RP turn loop, generalized.

## Inputs (`args`)

| arg | required | meaning |
|-----|----------|---------|
| `brief` | **yes** | the commission each worker drafts against |
| `sources` | **yes** | array of file paths — the ground truth verifiers refute against |
| `unit` | no | what one artifact is (`"beat"`, `"design proposal"`); default `"artifact"` |
| `workers` | no | independent drafts; default 3 |
| `angles` | no | per-worker perspective strings, cycled by index; default conservative/ambitious/skeptical |
| `verifierLenses` | no | default `["fabrication","contradiction"]`; extra lenses get generic adversarial framing |
| `requireCitations` | no | default true — an uncited factual assertion is a finding |
| `maxShortlist` | no | default 3 |
| `workerModel` / `verifierModel` / `rankModel` | no | model overrides (verifiers default `"sonnet"`) |
| `verifierAgentType` | no | custom agent type for verifiers — e.g. `claim-verifier`, whose Read/Grep/Glob allowlist makes the verifier read-only *structurally*, not just by instruction |
| `context` | no | extra working-context path (readable, but not a claim source) |

## Verdict rules

- Any `fabrication` / `uncited` / `misquote` / `contradiction` finding fails the draft.
- **Uncertainty fails**: a claim the verifier cannot confirm must not ride a pass into promotion.
- **A dead verifier fails the draft** (`missingVerdicts`): unverified work does not reach the shortlist.
- A sole survivor skips ranking; zero survivors returns an empty shortlist plus every reject's findings.
- Ranker misbehavior (empty/unknown indices) falls back to verification order — verified work is never
  silently discarded.

## Return value

`{ shortlist:[{rank, draft, angle, artifact, claims[], rationale}], rejected:[{draft, angle, findings[], missingVerdicts, artifact_excerpt}], stats:{workers, drafted, survived, lenses, requireCitations}, promotion_note }`

Deterministic by construction (angles cycle by index; no randomness) — resumable under the Workflow
runtime's caching. Wiring test: `tests/js/commission-wiring.test.mjs` executes the real engine body
against mocked primitives.
