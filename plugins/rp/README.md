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
| `Work/markers/rp-active` | session marker; records `session_file:` and `session_opened:`; `/rp:start` also appends a `<!-- rp-session-opened: <date> -->` line where the session opens, which `session` reports as `session_opened_line`; also gates the session plugin's per-turn routing nudge |
| `Work/rp/<campaign>/log/day-NNNN.md` | session record, one file per fiction day (see **Day files**); the marker, not the clock, says which is live |
| `Work/rp/rp_session_YYYY-MM-DD.md` | legacy session record, one per session, named by the day it starts; a campaign with no fiction day keeps it |
| `Work/rp/_validator_log.jsonl` | one line per gate step: `lint`, `skipped`, `review`, `adjudicate`, `turn`, `rotate`, `strike`, `ship` |
| `.claude/modes/rp.yaml` | optional `turnGate` and `sessionLog` blocks (below) |
| `Work/rp/{summaries,characters,pending-canon}/` | derived session artifacts; `summaries/<campaign>/day-NNNN.md` holds one summary per closed fiction day |
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
| `session` | prints the live session file (the marker's `session_file:`, else with a warning the newest open session: the head of a day-file chain, or the newest legacy `Work/rp/rp_session_*.md` unless `/rp:end` closed it or it moved to day files; an older legacy log is never offered, so another campaign opens with `/rp:start load <session>`; when nothing is open but `/rp:end` closed a legacy log that can still rotate, the warning names it to `load` with the opening day), the gate settings, where current state is (`live_state`), the continuity read (`continuity`: the last `previousBeats` beats, and the day's `plans`), the files this session wrote (`session_files`) and any warnings |
| `check` | lints one GM turn and routes it (ship, fix or review; a rewrite is always reviewed); appends `lint` (and `skipped`) lines |
| `rotate` | before a beat ships: names the file it goes to, rotating to the next fiction day's file when the day moved (see **Day files**), then writes the turn's struck drafts into it (see **Struck drafts**); `--start` records the session's opening in the marker; `--repair` only closes a file an interrupted rotation left open and writes struck drafts still waiting |
| `strike` | keeps a draft the gate rejected: stashes the turn file's DRAFT for `rotate` to write |
| `log` | appends a `review`, `adjudicate` or `turn` line |
| `invariants` | reports live state in `Memory/invariants.md` and its age against the run's state; `--strict` exits 1 on live state (the `/rp:extract-invariants` check) |
| `split` | read-only: plans how existing logs would cut into day files, every step-back, and (`--cite`) where each `rp_session_<date>.md:N` or `<MM-DD>:N` citation would land and whether the remap is exact |
| `ship` | after `rotate`: appends the shipped text (the new day's Day Plan, then the beat) and applies the gated delta to the frontmatter in one write; never a closed file; a retry ships once (see **Run state**) |
| `state` | reports the `run:` block's health; `--refresh` copies it into the run state file's generated block |
| `summary` | lists the days a session closed and their summaries; `--publish` checks and writes a closed day's summary once; `--stitch` writes the session summary from them |

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
keys are schema keys (no root mirrors), and its participants, time and day
(`currentDate` against the anchor's `Scene: Day N`) agree with the anchor; and every mention of a known cast member in narration (outside
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

## Day files

A campaign that counts fiction days (`currentDate: "Day 196 (Monday)"`) and
names a `log_dir` in its session frontmatter keeps one log file per fiction
day: `<log_dir>/day-0196.md`, the day zero-padded to four digits, so files sort
by day and each campaign has its own directory. A campaign without either keeps
one file per session, as before. A run reset moves the old run out of `Work/rp/`
(to `Work/archive/<campaign>-run-<date>/`, as the 2026-09-20 reset did) and gives
the new run a fresh `log_dir`: a day file stays open after `/rp:end`, so an old
run's last day file left under `Work/rp/` remains a fallback candidate, and the
new run's Day 1, written by hand, would overwrite the old run's `day-0001.md`
(`/rp:start` stops when that file exists; a later rotation refuses one).

A beat belongs to the day it ends on: the logs write the next day's Day Plan
before the night beat that reaches it. Its day is the delta's `currentDate`
(the lint holds it to the anchor's `Scene: Day N`), else the frontmatter's; the
beat heading is only checked against it, so a slip in the heading warns
instead of rotating. Before a beat ships, `/rp:turn` runs `rotate`. When the
day is later than the file's, rotation, in this order:

1. publishes the next day's file whole: the current frontmatter (with
   `log_day`, `continues` and `opened` set, `status` dropped) and the header
   between the frontmatter and the first Day Plan, copied as written (tier,
   register, GM line, standing facts, live workings and the trigger table,
   open threads), under a generated title and a "Continues `<file>`" line;
2. swaps the marker atomically to the new file;
3. closes the old file with a one-line footer pointing to the new one.

A closed file is never edited again, so its `file:line` citations stay valid.
An open file's lines move each time a delta grows or shrinks its frontmatter,
so cite a line only in a closed file (closed by rotation's footer; a legacy log's
`status: closed` does not count, since `/rp:start` can reopen it). While a file is open, cite the beat
(`B4161`), including the beat that set a header or frontmatter fact, never a
line or a header section; add the line once rotation has closed the file.
Outside the campaign's own directory a citation is path-qualified
(`garret/log/day-0197.md:12`), since every campaign has a `day-0197.md`.
Only a footer that ends a file closes it; a footer quoted elsewhere (a "Resume
point, verbatim") does not, and `session` warns about it.
Each step is safe to repeat: a re-run after a crash adopts the new file,
writes no second footer, and `/rp:turn`, `/rp:end` (`rotate --repair`) or the
next rotation closes a file a crash left without its footer. The marker never
goes missing, so the UserPromptSubmit hooks, which test only its existence,
are never switched off mid-rotation. An existing day file that does not
continue the current one (another run, or a closed file) is refused, not
overwritten. A beat on an earlier day than its file (a flashback or a numbering
slip) stays in the current file with a STEP-BACK flag in its heading: rotation
only moves forward. A legacy log's day is the highest its beats reached, so a flashback's delta
does not pull it back. The lint holds the delta's `currentDate` to the anchor's
`Scene: Day N` (the day its leading span ends on; a clock later in the field,
"THE CURE Day 3 of 30", is not it), and sends to review a day move the anchor
does not confirm (`delta_day_unanchored`) or a jump of more than 7 days
(`delta_day_jump`); `rotate` itself refuses such a jump unless the Keeper confirms it
(`--allow-jump`), since a typo would split the log past every day between, for good. A day
file a crash left between publish and marker swap, with nothing written after
its header (rotation records how many lines it wrote), is renamed to a hidden
`.day-NNNN.md.orphan-<date>` by the next `rotate` (never deleted).

Each turn the GM and the reviewer read the header and the last
`sessionLog.previousBeats` beats (default 20), wherever they fall, as line
ranges from `session` (`continuity`; `LIVE_STATE`'s `day` and `earlier`
lines). A day with that many beats is read from its last ones only, never
whole; while it holds fewer, the read takes the day so far (its Day Plan
included) and tops up from the beats before it: a legacy log's earlier beats,
then the files before, as far back as it takes. In a legacy log the day starts
with its first Day Plan or beat. `plans` names each Day Plan section of the day
(an ADDENDUM or a second session's plan too) that the window does not hold. A session can span several day files; `session_files` lists them
(and `session_opened_line` where the session began in the first) for `/rp:end`, whose summary and ratification cover them all and whose END
block goes in the current day file. A day file stays open after `/rp:end`; the
next `/rp:start` continues it on the same day or rotates from it.

```yaml
sessionLog:
  rotation: day        # day | off
  previousBeats: 20    # 0-200: the last beats the GM and the reviewer read, wherever they fall
```

Both keys are optional; a mode file without the block keeps the defaults.
`split` plans, without writing anything, how an existing log would cut into
day files and how each old citation would remap.

## Struck drafts

A draft the gate rejects is kept as an audit trail, as SillyTavern keeps swipes
beside the reply chosen: each draft a rewrite replaces, and a surrender's final
draft. It is never canon and never state. `/rp:turn` runs `strike`, which
stashes the turn file's DRAFT (the text the gate checked) under
`Work/rp/.gate/struck/`; `rotate`, which runs before every shipped beat and at
`/rp:start` and `/rp:end`, writes each stashed draft into the file it names,
before the beat, then clears it. The beat's file is known only when its final
draft ships (its day may rotate the log), so a strike never writes a log itself,
and never a closed one: a struck draft lands in the file its beat goes to. A
draft whose beat never shipped lands where its session went on: the current
file at `/rp:end` or at the next `/rp:start` (before a later opening day closes
that file), or the file that continues it when a session moved on without
`/rp:end`.

```markdown
> [!struck]- STRUCK, not canon: Beat 4174, attempt 1, struck 2026-10-26. Reason: presence, scene_state_mismatch
>
> <the draft, line by line, as the gate checked it>
>
> <!-- rp-struck: 3f9c0a1b2c3d4e5f -->
```

Why an Obsidian callout: every line starts with `>`, so the block ends itself
at the first line that does not, and a block cut short by a crash never takes in
the beat after it. No heading, beat, Day Plan, footer or marker pattern matches
a quoted line, so beat parsing, the day and rotation logic, the header copy and
the footer test never see one. Obsidian shows it folded, under a title that
says what it is. The key, a digest of beat, attempt and text, sits on the last
line: a re-run never writes a block twice, and a block cut short (no key) is
written again whole. The log already uses the word STRUCK for the Keeper's
rulings, so the type `[!struck]`, not the word, marks a block.

It costs play nothing: no model call, and nothing in the per-turn read. The
header range, the header copied into the next day file and every continuity
range skip struck drafts, as if they were not there; a range with one inside
lists its `parts`, the pieces to read. A struck draft never breaks a range or
ends the header, and a broken stash entry never stops a beat from shipping.
The ratifier, canon-writer, the `/rp:end` summary, `timeline-search` and
`world-lookup` exclude struck drafts, or label one struck wherever they show it;
a closed day's summary range skips them like every other range.
The edit pass and the mechanical fixes amend a draft rather than reject it, so
they strike nothing; a beat the Keeper strikes in chat after it shipped stays in
the log as played.

## Run state

A session may keep its PC's run state typed, in a `run:` block of its
frontmatter beside `scene:`:

```yaml
run:
  stats:                  # an integer, or "current/max"
    POW: "54/60"
  body:                   # one line of text per part, citing the beat that worked it
    antlers: "stumps (broken D160, D162; B3328, B3709)"
  workings:               # on or off
    FIGHTS THE BIT: on
    HOMING: off
  clocks:                 # the fiction day it fires, and what fires (time optional)
    vine-test: {day: 198, fires: "the vine test at the wall"}
```

The block is opt-in: without it, run state stays free text in the run state
file and the session header, and nothing here changes. With it, the GM's
SCENE_STATE_DELTA carries changes as `run.<section>.<name>` entries, and the lint
checks each one: a known section, an existing stat (only the Keeper adds one),
the stat's type as it stands, a body fact as one line of text, a working
toggled only to `on` or `off` (a new working lands `on`; a new one turned `off`
is a misspelling), and a clock as `{day, fires}` with its day between the
beat's day and a year ahead. These findings are mechanical: the edit pass fixes
them, one it leaves never sends a beat to the reviewer, and `ship` does not
apply it. The reviewer sees `run.*` entries only in the deltas of beats already
routed to it, and checks them against the prose (`scene_state_mismatch`).
A clock past its day that no delta has fired or cleared is a note in `check`
and `session`, never a finding.

`ship` writes the beat and its delta in one replace of the open file, and
records `shipped: "<beat>@<digest>"`: no state lands without its beat or the
reverse, a retry after a crash ships once, a closed file is refused, and a
concurrent hand edit is kept (`ship` starts again from the fresh file).
Rotation copies the block into each day file, so each closed day file's
frontmatter holds the state at the end of its day.

After a rotation, `state --refresh` copies the block into the run state file
(the session's `run_state:` pointer) as its section's first entry, between
`<!-- rp-run-state ... -->` markers that record its digest, and puts it back
first when a prose entry was written above it. Every other line is kept; a
block edited by hand is not overwritten (the refresh refuses and says so); the
block is never written into a session log or the invariants. The block is a
copy: the frontmatter governs, and `session` warns when the section's current
entry is hand-written prose while a `run:` block exists. With the block first,
the per-turn read skips the run state file (the frontmatter holds the state),
so a standing ruling or steer the GM must see each turn belongs in the session
header, which every turn reads, not in a prose entry of the run state file.

`ship` rewrites only the frontmatter lines its delta changes: every other line
of a block, its comments, blank lines and values the gate's YAML subset does
not model (floats, block scalars), stays as written. It then reads the whole
frontmatter back and refuses, writing nothing, if any key would read
differently from what it meant (a value the subset cannot hold, a line it
cannot place). Bytes it does not change are kept: a BOM, CRLF line ends, bytes
that are not UTF-8.

## Day summaries

When rotation closes a fiction day, `/rp:turn` writes that day's summary
after the beat is shown: one haiku call (`timeline-search`, DAY SUMMARY) over
the day's beats and Day Plans, struck drafts left out (`parts`), then
`summary --publish`, which writes
`Work/rp/summaries/<campaign>/day-NNNN.md` (the campaign directory is the
`log_dir` under `Work/rp/`, without its trailing `/log`) under generated
frontmatter, once. It refuses a summary that cites a line in a file still open
(cite beats; a line only in a file rotation has footered), a line of a struck
draft, a beat after the day's highest, or the wrong `## Day N` heading; a
different text for a day already summarised is refused, never overwritten.
Only `<file>.md:<line>`
citations without spaces in the path are checked; the logs' short forms
(`10-24:42`) are not. A day closed by `/rp:start`, before the session's own
files, is due all the same: `summary` (run at each rotation and at `/rp:end`)
walks back through every closed day of the run, at most 64, and lists each
without its summary; `session`'s `summaries_due` stops at the first summarised
day, so it costs a hop or two per turn. A closed day's range stops before a
later day's Day Plan written early into its file. `/rp:end` then writes only
the open day's part and the session's own notes, and `summary --stitch`
assembles the session summary: the day summaries in day order, those notes,
and last the GM's own "State at close" (place, gear, what typed state does not
hold) with a copy of the `run:` block under it. `session` lists closed days
still without a summary (`summaries_due`).

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
