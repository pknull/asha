# Draft-Loop Craft Core — SHARED across all profiles

> Shared, content-agnostic craft auto-fails + generative directives inherited by **every** draft-loop profile (rp, hush, future, and every project). The engine (`plugins/write/engines/rp-draft-loop.js`) feeds this file to **every** profile's PROSE/solo drafter (via `SOURCES`) and to the **CRITIC** + **solo self-audit** as scoring rules. It does **not** feed the CONTINUITY agent (these are voice/craft, not continuity). Profile rubrics (a project's `reference_<mode>_draft_loop_rubric.md`) carry domain-specific rules **in addition** to these. On any conflict, the stricter rule wins.
>
> **Portable:** this file ships with the asha `write` plugin. A project's mode manifest binds `slots.craftCore` to it (conventionally `${asha}/craft/craft-core-universal.md`), so every project inherits the same universal craft layer; the project supplies only its profile-specific rubric, voice, and bible.

These are **content-agnostic**. They describe *how prose manages tension, state, and pacing* — not *what it is about* — so they apply to RP beats, novel passages, romance, literary fiction, anything.

---

## CRITIC auto-fails — universal (any one = FAIL)

### Tension / resolution

- **`both_true_resolution`** — A beat resolves its tension by asserting opposed truths hold *simultaneously*: "X and Y at once," "both true," "and it always would be," "the tenderness and the cruelty in the same breath," "real and impossible at the same time." **Trigger:** used as a **beat-closer**, OR more than **once per scene**. Contradiction belongs *across* beats, not blended within one. **Fix:** let ONE thing stand in the moment; put its opposite in a different beat and let the reader hold both. (Genuine ambivalence rendered *once* and *earned* is allowed — this targets the habit, not the tool.)

- **`narrated_contradiction`** *(dialogue)* — A character states or explains their own paradox, motive, or both-sidedness aloud: "I love you and I'm the one doing this to you," "I'm cruel to you because I care." **Why it fails:** self-narrated contradiction reads as self-exculpation / bad faith — the character performing a stable position instead of living an unstable one. **Fix:** cut the explanation. The character commits to the present feeling, fully; the contradiction shows in the *gap* between this line and a later, opposite action — never spoken.

- **`editorializing_close`** — A beat ends on an interpretive / summary sentence that reconciles or explains what just happened ("and that was the whole of it," "which was love, and was also a cage," "he understood then that…"), instead of ending on the concrete, the action, or the cut. **Fix:** end on the image, the act, the line, or nothing. Reconciling is the reader's job. (This also catches narrator hedging — stepping out to frame the material as it lands.)

### Pacing / rushing — the anti-rush family

> The single most common failure: once the writer sees where a beat is going, the prose-drive toward resolution compresses the journey and "lands" the idea instead of *living in the approach.* Tension dies because the ending was shown early. These catch it.

- **`telegraphed_destination`** — The outcome is pre-narrated — named, foreshadowed sentimentally, or shown in setup/exposition — so the reader/player sees the ending before the approach is traversed ("she knew where this was going," "it would end the way these things end," the narrator gesturing at the payoff before it arrives). **Why it fails:** a destination shown is a destination spent; nothing is left to dread or discover. **Fix:** withhold the destination entirely; render only the present step, and let the arrival, when it comes, *be* the first time it's seen.

- **`arrived_not_approached`** — The prose jumps to the payoff (the climax, the realization, the transformation, the capitulation) without traversing the path to it — a state or sensation escalates with no graduated build, the moment delivered whole instead of approached. **Why it fails:** the payoff is unearned and the reader is a spectator to a result, not a participant in a process. **Fix:** render the approach in increments and **stop short of the arrival**; the payoff is a *later* beat.

- **`rushed_increment`** — Multiple distinct steps that each deserve their own beat are compressed into one — build and climax in the same breath, three escalations summarized as one, a process collapsed to its outcome ("arms, pants, done"). **Why it fails:** each step the reader doesn't get to inhabit is a step of tension thrown away. **Fix:** break the compression apart; give each genuine increment its own dwell.

- **`dwell_deficit`** *(profile sets severity)* — A high-stakes moment is given far fewer words / beats than its weight demands — a key beat skimmed (e.g. under ~½ the length of comparable moments in the same work), a turning point passed over in a sentence. **Why it fails:** the prose advances when it should deepen; the reader has no time to land in the state before the next push. **Fix:** weight the words to the moment; let the important beat be long and slow.

---

## Craft rules — universal definition, profile sets the severity

These failures are shared across profiles; the **definition lives here** (maintained once), but whether a profile treats one as auto-fail or minor is set in that profile's rubric, which also carries the domain-specific instance and detection.

- **`exposition_control`** — Backstory, mechanism, cosmology, or world-info delivered as a *block*, an *announcement*, or a *gloss/decode*, instead of surfaced through action / object / friction — or left withheld where withholding is the design. **Fix:** install it through routine and let it be discovered; never declare it. *(Profile instances: RP `exposition_dump` — the cultivation announced; Hush `exposition_decode` — the cosmology / untranslated German glossed.)*
- **`abstract_sensation`** — An emotion, sensation, or action met with a *named label* ("he felt X," "it activates," "responds," "violated") instead of a specific *rendered* concrete. **Fix:** render the physical particular; cut the label. *(Profile instances: RP `abstract_body_response`; Hush minor "slack abstraction where a concrete image was available.")*
- **`uniform_rhythm`** — Sentence rhythm flattened: uniform length and shape, no variance; at the structural extreme, smooth even beat-architecture (the GPTZero template). **Fix:** vary length and shape — a long clause against a one-word line; break the even cadence. *(Profile instances: RP minor `uniform_rhythm`; Hush auto-fail `rhythm_flatness`.)*

---

## Generative directives — universal (for the PROSE / solo drafter)

- **Pacing intent first — approach, don't arrive.** Before drafting, fix the pacing intent: name where this beat is *going*, then render only the **approach** and **stop short of it.** The payoff is earned across beats, never delivered in one. When you can see the destination, that is the signal to slow down, not speed up — the destination is the thing you withhold.
- **Default to DEEPEN, not ADVANCE.** A player turn / a new line is a reason to go *deeper into the present moment* by default — more sensation, more friction, more of what is already happening — unless it explicitly pushes the situation forward. Advancing the plot is the exception; dwelling is the rule. (Match the *unit*: in interactive RP, one input usually deepens one beat — do not consume three steps of the arc in one reply.)
- **Singular states, sequenced.** In any beat a character occupies *one* state fully, and it may exclude its opposite. Range and movement come from traversing distinct states *across* beats — contradiction across time, not superposition.
- **Watch for the fixed point.** If every beat lands in the same emotional resolution regardless of what happens in it, the state-space has collapsed and the prose is dead however violent the events. Vary the landing; let beats end badly and *stay* there.
- **Falseness is load-bearing.** Allow lies that stay lies, hopes that are wrong, losses that are total, moments that are only one thing. If nothing can be false, nothing is at stake.
- **Reconciling is the reader's job.** Render singular scenes and trust accumulation. Don't state the paradox; don't footnote the beat.

---

## Profile mapping (reference)

Single source of truth for each universal kernel; the profile rubrics keep the domain instance, detection, and severity, and point back here. The rows below document the AAS reference profiles (rp, hush) as the worked example; a new project adds its own profile rows in its own rubric.

| Core rule | RP category | Hush category |
|---|---|---|
| `both_true_resolution` | (enforced via core) | cf. `tautological_recursion` |
| `narrated_contradiction` | (enforced via core) | cf. `tautological_recursion` |
| `editorializing_close` | (enforced via core) | cf. `resolution_creep` (related, not identical) |
| `telegraphed_destination` | (enforced via core; RP detection in rubric) | (enforced via core) |
| `arrived_not_approached` | (enforced via core; RP detection in rubric) | (enforced via core) |
| `rushed_increment` | (enforced via core; RP detection in rubric) | (enforced via core) |
| `dwell_deficit` | RP detection in rubric (severity) | (enforced via core) |
| `exposition_control` | `exposition_dump` | `exposition_decode` |
| `abstract_sensation` | `abstract_body_response` | "slack abstraction" (minor) |
| `uniform_rhythm` | `uniform_rhythm` (minor) | `rhythm_flatness` (auto-fail) |

**Deliberately NOT migrated (stays profile-specific):** RP `tic_density` (literal substring scan — the tuning is the specificity), `tag_word_labeling`, `smooth_at_recognition`, `telegraphing`, and `flatness_engine` (a *dramatic-stakes* rule — **not** merged with Hush's rhythm flatness); Hush `register_break`, `tautological_recursion`, `resolution_creep`.

> **Pacing note:** the anti-rush auto-fails above are the *hard floor* (any one = FAIL). The richer pacing assessment — dwell weighting, texture, state-settling — is the job of the optional **Director reviewer** (`plugins/write/craft/director-rubric.md`), enabled per-manifest via `slots.directorRubric`.
