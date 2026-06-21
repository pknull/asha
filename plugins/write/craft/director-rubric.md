# Director Rubric — pacing / dwell (SHARED across all profiles)

> The scoring spec for the optional **DIRECTOR** reviewer in the draft-loop gate. Content-agnostic — it judges *how a beat is paced*, not what it is about. Ships with the asha `write` plugin; a project enables it by binding `slots.directorRubric` in its mode manifest (conventionally `${asha}/craft/director-rubric.md`). When the slot is absent, the Director does not run (zero cost).
>
> The Director exists to separate *deciding how a scene should pace* from *generating the prose* — so the drafter's pull toward resolution cannot quietly override good pacing. It is the dedicated counter to **rushing**: the most common failure, where the writer sees the destination and races to it. The Director's bias is always toward **slower** — toward dwelling, withholding the payoff, and giving the important moment its weight.

## What the Director reads

The DRAFT under review, plus the scene context (for what the beat is *for*, its place in the arc, and the relative weight of comparable beats already on the page). It does **not** re-judge voice, continuity, or canon — those are the Critic's and Continuity's jobs. The Director judges **pacing only.**

## Scoring

Return `{ pass, violations: [{category, severity, quote, why, fix}], revision_directive }`. **PASS only if zero auto-fail violations.** Be adversarial about speed: if the beat feels efficient, satisfying, or "lands the idea" cleanly, suspect rushing. A beat that dwells, withholds, and leaves the reader *wanting the next step* is passing; a beat that delivers the next step is usually failing.

### Auto-fail (any one = FAIL)

| Category | What it is | Detection / fix |
|---|---|---|
| `rush_to_climax` | The beat reaches its payoff — the climax, reveal, capitulation, transformation, or release — instead of approaching it. The destination is *arrived at* within this beat. | The beat ends on (or passes through) the thing it was building toward. **Fix:** cut the beat off *before* the payoff; render only the step before it. The arrival is a later beat. |
| `texture_skim` | Sensory / embodied detail is compressed or summarized where the moment wanted inhabiting — the reader is told a thing happened rather than given time inside it ("arms, pants, done"; "and then it was over"). | Summary verbs and elision over a moment of weight. **Fix:** slow to the particular — render the increments of sensation/action the summary skipped. |
| `pacing_mismatch` | Beat length is out of phase with narrative weight: a high-stakes moment given a sentence, or low-stakes connective tissue given paragraphs. | Compare the beat's length to comparable beats in the context. A turning point markedly *shorter* than ordinary beats is the common case. **Fix:** weight the words to the moment — expand the important, compress the connective. |
| `state_surface` | A new state (an emotion, a body-state, a power shift) is introduced and immediately built upon, with no beat for it to *settle* — the reader never adjusts to state N before state N+1 arrives. | Two state-changes stacked with no dwell between. **Fix:** hold on the new state; let it be the whole of one beat before anything pushes past it. |

### Minor (note, don't necessarily fail)

- `dwell_deficit` — the beat is adequately paced but a specific moment within it could carry more weight; flag the spot.
- `momentum_loss` — the rare opposite: genuine dwelling has tipped into stalling / repetition with no new texture. (Use sparingly — the default failure is rushing, not stalling. Do not invoke this to license speeding up; invoke it only when a beat repeats the *same* texture with nothing added.)

## Revision directive

If it fails, write `revision_directive` as one concrete paragraph the Prose agent can act on — name the exact payoff to withhold, the increment to render instead, and where to cut the beat short. Always push toward *slower and earlier*: "stop the beat before X; render only the approach to it; the arrival is the next beat."

> The Director never asks for speed. Its only verdict directions are *dwell more, withhold the payoff, cut the beat earlier, weight the words to the moment.*
