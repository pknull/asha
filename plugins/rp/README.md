# RP Plugin

**Version**: 0.4.0

Live-interactive roleplay: session lifecycle, per-turn continuity gating, canon ratification, and world/history lookup. The *verbs* of running a game live here; the *nouns* — canon, character registers, rulesets, the invariants projection — stay in the consuming project.

Complements the `write` plugin, which owns the shared craft layer (`craft/craft-core-universal.md`, `craft/director-rubric.md`), the `rp-draft-loop` engine, and the `continuity-reviewer` agent this plugin's turn loop spawns.

## When to use it

Use RP for live, turn-by-turn play where continuity must be checked before each
response and session events must pass an explicit canon gate afterward. Use the
Write plugin instead for offline manuscript drafting or editorial review.

## Invocation by harness

| Harness | Invocation |
|---|---|
| Claude Code | `/rp:start`, `/rp:turn`, `/rp:end`, `/rp:extract-invariants` |
| OpenAI Codex | Request or name the rendered `rp-start`, `rp-turn`, `rp-end`, or `rp-extract-invariants` skill |
| GitHub Copilot CLI | Request or name the same rendered skills |

## First-time project setup

1. Install both `rp` and `write`.
2. Copy `templates/canon-layout.md` to the project's `Memory/canon-layout.md`.
3. Edit the register so every canon kind resolves to the project's real paths.
4. Run `/rp:extract-invariants` after the canon sources exist.
5. Start the session with `/rp:start`.

Do not begin live play with an inherited path layout that does not match the
project. Empty lookups then masquerade as absent canon, which is a particularly
tedious species of error.

## Session lifecycle

```text
/rp:start Garret 1889-10-03       open markers, retrieve continuity, build Day Plan
/rp:turn I open the cellar door.  run one GM draft through the continuity gate
/rp:turn I ask her what changed.  repeat for each Keeper turn
/rp:end                            summarize, update character state, ratify canon
```

`/rp:turn` lints every draft deterministically and spends a model reviewer only
on beats the lint flags or that are consequential (see **Turn gate**). A
confirmed violation gets one rewrite on a routine beat and three on a
consequential one; if the draft still violates the invariants or source
provenance, it surrenders rather than shipping known-bad continuity. Each turn
returns exactly one complete beat, never cuts off mid-dialogue, and closes with
one Scene/Location/Present anchor line.
`/rp:end` presents proposed canon additions individually;
only accepted items are promoted.

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
| `/rp:turn` | Run one turn through the continuity gate (draft → lint → review if flagged or consequential → rewrite within the cap → ship or surrender) |
| `/rp:end` | Close a session — summary, character state, ratification, optional canon promotion |
| `/rp:extract-invariants` | Rebuild the invariants projection from canonical sources |

## Project layout assumed

Path conventions, matching the `write` plugin's posture (plugin content addresses project-relative paths directly; the installer does not rewrite markdown):

| Path | Role |
|---|---|
| `Work/markers/rp-active` | session marker; records `session_file:`; also gates the session plugin's per-turn routing nudge |
| `Work/rp/rp_session_YYYY-MM-DD.md` | session record, named by the day it starts; the marker, not the clock, says which is live |
| `Work/rp/_validator_log.jsonl` | one line per gate step: `lint`, `skipped`, `review`, `adjudicate`, `turn` |
| `.claude/modes/rp.yaml` | optional `turnGate` block (below) |
| `Work/rp/{summaries,characters,pending-canon}/` | derived session artifacts |
| `Memory/invariants.md` | the compiled invariants projection the per-turn gate reads: static canon only |
| *(the run's state file)* | named by the session's `run_state:` frontmatter (or a ``State: `<path>` § <section>`` header line); with the session's frontmatter and header, the source of current state |
| `Memory/canon-layout.md` | **the canon register** — maps entity kind → project path. Copy from `templates/canon-layout.md` |
| *(paths named in the register)* | authored canon — the source of truth the projection is compiled *from* |

## Canon layout — the nouns stay in the project

The plugin ships the *verbs*. Where a project keeps its characters, places, and artifacts is a *noun*, and it belongs in `Memory/canon-layout.md` — copied from `templates/canon-layout.md` and edited to match the project.

Agents (`world-lookup`, `canon-writer`, `timeline-search`, `character-template`) and commands (`/rp:end`) resolve every canon path through that register, falling back to a documented default only when it is absent.

**Why this is enforced rather than suggested.** Until v0.2.0 these agents addressed one vault's tree directly (`Lore/World/Places/`, `Lore/World/Magic/Artefacts/`, …). In any project that did not share that layout, every lookup resolved nothing — and an empty result is indistinguishable from "no such canon exists". The plugin did not fail loudly; it quietly reported absence. Three separate path vocabularies had also drifted apart inside the plugin itself (`Places/` vs `Locations/`, `Magic/Artefacts/` vs `Items/`), which is what an unowned convention looks like after a few edits.

The same rule covers the direct `rp-priced-stakes` prompt safeguard: its match
language must not contain proper nouns from one campaign. The safeguard is
marker-gated by `rp-active`, uses a 1 h project-local cooldown, and can be
disabled with `Work/markers/rp-priced-stakes-off` (the historical
`nudge-rp-priced-stakes-off` alias remains honored).

## Turn gate

`tools/rp_gate.py` (standard library, no model call) backs the gate:

| Subcommand | Use |
|---|---|
| `session` | prints the live session file (the marker's `session_file:`, else the newest open `Work/rp/rp_session_*.md` with a warning; `/rp:end` marks a session `status: closed`, which the fallback skips), the gate settings, where current state is (`live_state`) and any invariants warning |
| `check` | lints one GM turn and routes it (ship, fix or review; a rewrite is always reviewed); appends `lint` (and `skipped`) lines |
| `log` | appends a `review`, `adjudicate` or `turn` line |
| `invariants` | reports live state in `Memory/invariants.md` and its age against the run's state; `--strict` exits 1 on live state (the `/rp:extract-invariants` check) |

**Current state never comes from the invariants.** `session` reports it as line
ranges for the GM and the reviewer to read: the session file's frontmatter and
header (up to the first Day Plan or beat heading, at most 250 lines), and the
run's state file the session names, with the first entry of its section as the
current one. The pointer is the frontmatter `run_state: "<path>#<section>"`
(`/rp:start` copies it from the canon register's `run_state` row), else a
``State: `<path>` § <section>`` or ``Run state: `<path>` `` line in the header;
the section defaults to `Run state`. A path outside the project, or the
invariants themselves, is not read. The run's state is dated by the newest date
in that current entry (dates after today are ignored); the session's start date
stands in only when no such date resolves, since opening a session changes no
canon.

`session` and `check` warn, and the lint line records, when the invariants
carry live state (`live_state`) or when `last_extraction` predates the run's
state (`stale`). Live state is a Timeline State, Run state, Live state, Current
state, Live clocks, Current vessel or Resume point heading (numbering ignored);
a bold or plain label opening a line with one of those names except Live state
and Current state ("**Run state (Day 60)**:", "- Live clocks:"); a locked name
ending in one; or a snapshot phrase ("now at Day 60", "now at D195", "resumes
Beat 343", "resumes B4100"). Protocol words such as "Scene state" are not live
state, and closed code fences are skipped. Each hit says whether it sits in
KEEPER-LOCKED content (`locked`), which `/rp:extract-invariants` never edits.
The checks are text patterns, not judgement: a rule's worked example that
embeds a current value ("90 vs 59 = 81%"), or a snapshot in a table row, is not
caught. Reading the invariants and the run state adds 1–3 ms to a `check`.

The lint checks what is mechanical: exactly one closing anchor; SCENE_STATE_DELTA
keys are schema keys (no root mirrors), and its participants and time agree
with the anchor; and every mention of a known cast member in narration (outside
dialogue) is someone in the scene, in the anchor's `Present:` list, arriving on
the page or sending word. A mention is let off only when it is plainly not that
person on the page: a possession ("Velathra's plans"), "the Vey brothers",
"thinks of" or "word from", reported belief or speech ("Liss knows Vey...",
"Liss says Vey..."), or a stated absence ("is away", "will be back", "used to").
Names come from the session frontmatter, the anchor, the `character` row of
`Memory/canon-layout.md` (default `Lore/World/Characters/*.md`) and
`turnGate.cast`; a character-file name that is also a common word ("Frost") is
skipped at the start of a sentence unless it is in the scene or in
`turnGate.cast`. The default leans toward flagging: a false flag costs one
cheap review, a miss ships a beat unreviewed. It reads only the session
frontmatter, so log size does not matter: on 204 real beats it took about 7 ms
a beat (p95 17 ms), and a `check` call, Python start-up included, about 90 ms.

A beat is **consequential** when the check finds a priced stake (SOURCE_LOG,
or the priced-stakes phrases in the Keeper's input), proposed canon or a PC
change (SOURCE_LOG `canon_proposed`, `pc_changed`, or a new PC role in the
delta), an arrival or departure, a fight or capture, `[validate]`, or a
`consequentialPatterns` match. Otherwise it is **routine**.

The project's rp mode file may carry a `turnGate` block. Every key is
optional; an absent block, or a mode file without one, uses the defaults:

```yaml
turnGate:
  review: consequential     # consequential | every | lint-only
  rewrites:
    routine: 1              # 0-3
    consequential: 3        # 0-3
  models:                   # haiku | sonnet | opus
    gm: sonnet              # roleplay-gm drafts and rewrites
    reviewer: haiku         # the routine review pass
    adjudicator: sonnet     # confirms a hard violation before a rewrite, then reviews rewrites
  cast: []                  # extra names the presence lint should know (minor NPCs)
  deltaKeys: []             # extra SCENE_STATE_DELTA keys
  consequentialPatterns: [] # extra case-insensitive regexes, single-quoted
```

`review: consequential` reviews flagged and consequential beats; `every`
reviews every beat (the 0.3 behaviour); `lint-only` reviews only `[validate]`
beats. Block or flow style both parse (`rewrites: {routine: 1}`). An invalid
value falls back to its default with a warning in the check output. The
`models` keys of the draft-loop engine are separate and unchanged.

Per-spawn model choice is a Claude Code Task parameter. Codex, Copilot and
OpenCode were not checked: where a spawn ignores `model`, the routine pass and
the adjudicator run on the same model.

## Design note — projection vs source

`Memory/invariants.md` is a **generated projection**, not a bible: it is compiled for read-speed at turn time and is therefore lossy by construction. A claim that falls outside it is invisible to the gate rather than wrong. Treat it as a cache over authored canon — never as the only authority consulted.

It holds static canon only. A snapshot of play in it goes stale within a beat and grades every later draft against the wrong world (one projection still described Day 60 at Day 195), so current state is read from the session and the run's state file at every turn instead.

## Installation

```bash
./install.sh --only write --target claude
./install.sh --only rp --target claude
```

Substitute `codex` or `copilot` for the target harness. The RP workflow depends
upon shared Write-plugin craft and continuity surfaces, so install both.

## License

MIT
