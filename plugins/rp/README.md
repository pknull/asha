# RP Plugin

**Version**: 0.1.0

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
| `Lore/**` (or project equivalent) | authored canon — the source of truth the projection is compiled *from* |

## Design note — projection vs source

`Memory/invariants.md` is a **generated projection**, not a bible: it is compiled for read-speed at turn time and is therefore lossy by construction. A claim that falls outside it is invisible to the gate rather than wrong. Treat it as a cache over authored canon — never as the only authority consulted.
