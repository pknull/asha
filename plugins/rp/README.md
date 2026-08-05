# RP Plugin

**Version**: 0.2.0

Live-interactive roleplay: session lifecycle, per-turn continuity gating, canon ratification, and world/history lookup. The *verbs* of running a game live here; the *nouns* — canon, character registers, rulesets, the invariants projection — stay in the consuming project.

Complements the `write` plugin, which owns the shared craft layer (`craft/craft-core-universal.md`, `craft/director-rubric.md`), the `rp-draft-loop` engine, and the `continuity-reviewer` agent this plugin's turn loop spawns.

## Agents

| Agent | Role |
|---|---|
| `roleplay-gm` | Panel referee and scene driver: environment, NPC agendas, pacing, spawn discipline. Delegates profiled-NPC voice to character agents; never authors them |
| `rp-ratifier` | Session-end canon gate — diffs proposed additions against the invariants projection and surfaces each to the Keeper for accept / reject / defer |
| `canon-writer` | Promotes *ratified* items into project canon, matching existing file patterns |
| `timeline-search` | Chronological search across session records and summaries for continuity reference |
| `world-lookup` | Resolves proper nouns against project canon plus session-pending material |
| `character-template` | Generic self-sufficient NPC-voice agent; projects copy it per character |

## Commands

| Command | Purpose |
|---|---|
| `/rp:start` | Open a session — marker, continuity retrieval, Day Plan, scene state |
| `/rp:turn` | Run one turn through the continuity gate (draft → review → rewrite ≤3 → ship or surrender) |
| `/rp:end` | Close a session — summary, character state, ratification, optional canon promotion |
| `/rp:extract-invariants` | Rebuild the invariants projection from canonical sources |

## Project layout assumed

Path conventions, matching the `write` plugin's posture (plugin content addresses project-relative paths directly; the installer does not rewrite markdown):

| Path | Role |
|---|---|
| `Work/markers/rp-active` | session marker; also gates the session plugin's per-turn routing nudge |
| `Work/rp/rp_session_YYYY-MM-DD.md` | session record |
| `Work/rp/{summaries,characters,pending-canon}/` | derived session artifacts |
| `Memory/invariants.md` | the compiled invariants projection the per-turn gate reads |
| `Memory/canon-layout.md` | **the canon register** — maps entity kind → project path. Copy from `templates/canon-layout.md` |
| *(paths named in the register)* | authored canon — the source of truth the projection is compiled *from* |

## Canon layout — the nouns stay in the project

The plugin ships the *verbs*. Where a project keeps its characters, places, and artifacts is a *noun*, and it belongs in `Memory/canon-layout.md` — copied from `templates/canon-layout.md` and edited to match the project.

Agents (`world-lookup`, `canon-writer`, `timeline-search`, `character-template`) and commands (`/rp:end`) resolve every canon path through that register, falling back to a documented default only when it is absent.

**Why this is enforced rather than suggested.** Until v0.2.0 these agents addressed one vault's tree directly (`Lore/World/Places/`, `Lore/World/Magic/Artefacts/`, …). In any project that did not share that layout, every lookup resolved nothing — and an empty result is indistinguishable from "no such canon exists". The plugin did not fail loudly; it quietly reported absence. Three separate path vocabularies had also drifted apart inside the plugin itself (`Places/` vs `Locations/`, `Magic/Artefacts/` vs `Items/`), which is what an unowned convention looks like after a few edits.

The same rule covers shipped hook rules: a `match_regex` must not contain proper nouns from one campaign. A rule keyed to one setting's vocabulary fires there and stays silent everywhere else. Projects extend `rp-priced-stakes` by redefining the row in `~/.asha/nudges.json` (merged by id).

## Design note — projection vs source

`Memory/invariants.md` is a **generated projection**, not a bible: it is compiled for read-speed at turn time and is therefore lossy by construction. A claim that falls outside it is invisible to the gate rather than wrong. Treat it as a cache over authored canon — never as the only authority consulted.
