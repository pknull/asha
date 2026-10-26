---
name: rp-start
description: "Open an interactive roleplay session — sets the rp-active marker, retrieves continuity, builds a Day Plan and scene state, and deploys the roleplay-gm orchestrator."
argument-hint: "[character names | fiction date | 'load <session>']"
allowed-tools: ["Task", "Read", "Write", "Edit", "Bash", "Grep", "Glob"]
---

# /rp:start — Open a Roleplay Session

Initiates an interactive roleplay session by deploying the multi-agent `roleplay-gm` orchestrator.

## What This Does

1. **Marks RP mode active** via `rp-active` for RP routing and safeguards, and records the session file in it
2. **Retrieves session continuity** via timeline-search subagent
3. **Builds initial scene state** in session file
4. **Deploys roleplay-gm orchestrator** with subagent coordination
5. **Sessions logged to**: one file per fiction day, `<log_dir>/day-NNNN.md` (default `log_dir`: `Work/rp/<campaign>/log`), the day zero-padded to four digits. A session may span several day files; `/rp:turn` closes each day's file and opens the next as the fiction day moves on. A campaign with no fiction day (`Day N`) keeps one `Work/rp/rp_session_YYYY-MM-DD.md` per session, named by the real day it starts

## Project conventions

This command carries no campaign of its own. The consuming project supplies:

| What | Where |
|---|---|
| Default scene / cast, if any | project `CLAUDE.md` or the RP mode manifest |
| PC and NPC canon | `Lore/World/Characters/` (or project equivalent) |
| The invariants projection | `Memory/invariants.md` |
| Era, setting, and tone constraints | the invariants projection — **not** this file |

**Read the project's PC canon before rendering anything.** Era and setting limits on what a character can know come from the project's own canon, never from a default baked in here.

## Session Initiation Protocol

### Step 1: Choose the session file

- `load <session>` → that file (under `Work/rp/`).
- Otherwise find the run's open file:

  ```bash
  ASHA_ROOT="${ASHA_ROOT:-$(jq -r '.asha_root // empty' "${ASHA_HOME:-$HOME/.asha}/config.json" 2>/dev/null)}"; python3 "$ASHA_ROOT/plugins/rp/tools/rp_gate.py" session --project-dir .
  ```

  With no marker it prints the newest open session as `path` (`source: latest`): the head of the campaign's day files (a day file stays open after `/rp:end`; only rotation closes it), or the newest legacy `Work/rp/rp_session_*.md` when it is not closed and has not moved to day files. An older legacy log is never offered (none was ever closed), so a vault with several campaigns opens the other one with `load <session>`. A day-file campaign continues the file printed. Read its last scene state for continuity (from its last beat: a `> [!struck]` block is a rejected draft, never scene state). When it prints `source: new` and the warning names a closed legacy log that can still rotate (`/rp:end` closed it before its day rotated), continue that run from it: `load` the file it names and pass the opening day to Step 2. A later day closes it and opens the day file; on its own day it reopens the log, and play goes on there until the day moves. Never write that run's first day file by hand: rotation copies the header, sets `continues:` and footers the old log, and the continuity read needs all three.
- A legacy campaign moving to day files: add `log_dir` to its frontmatter (one line, between turns), keep playing in the legacy log, and let rotation open the first day file: Step 2's `rotate --start --day N` with a later opening day, or the first beat whose day moves on. Never write that first day file by hand: rotation copies the header, sets `continues:`, footers the legacy log, and the continuity read needs all three.
- A new campaign: the first day file is `<log_dir>/day-NNNN.md` for the opening fiction day, and its frontmatter names `log_dir` (Step 5). If that file already exists, stop: it belongs to another run (a run reset moves the old run out of `Work/rp/` first), and writing it would overwrite that run's log.
- A campaign with no fiction day: today's `Work/rp/rp_session_YYYY-MM-DD.md`. A new session that continues a closed one starts today's file and names the old one in its header. If today's file already exists, never write over it: `load` it (a closed log reopens) or ask the Keeper.

### Step 2: Mark RP Mode Active and record the session file

```bash
ASHA_ROOT="${ASHA_ROOT:-$(jq -r '.asha_root // empty' "${ASHA_HOME:-$HOME/.asha}/config.json" 2>/dev/null)}"; python3 "$ASHA_ROOT/plugins/rp/tools/rp_gate.py" rotate --start --project-dir . --session-file "<the file from Step 1>" --day <the opening fiction day, if the campaign counts days>
```

The file must exist: for a brand-new session file, do Step 5 first, then run this step, then go on from Step 3.

`rotate --start` writes the RP routing/safeguard marker (`session_file:` and `session_opened:`) and appends a `<!-- rp-session-opened: <date> -->` line to the file the session opens in, which `/rp:end` reads through `session` (`session_opened_line`). An opening day more than 7 days after the file's is refused like a turn's: ask the Keeper, and only on the Keeper's word run it again with `--allow-jump`. When the opening day is later than the file's, it first closes that file with a footer and opens the next day's file with the header copied as written, and the marker names the new file (`action: rotated`, `path`). Use the printed `path` as the session file from here on. A rotation here closes a day, as one in `/rp:turn` does: refresh the state block and summarise the closed day as `/rp:turn` Step 3e describes (`summary` lists it among its `days`, though the session opens after it). On `refused` (exit 1: a day file of that name belongs to another run or is closed) no marker is written: halt and show the Keeper the warning. Any other error is printed as is; fix what it names rather than repeating the command. `/rp:turn` and `/rp:end` read the session file from the marker (`rp_gate.py session`), because one session can run across several real and fiction days. Mechanical recovery remains independent.

### Step 3: Retrieve Session Context

Spawn **timeline-search** agent to get continuity:

```yaml
Task:
  subagent_type: "timeline-search"
  model: haiku
  prompt: |
    QUERY: "last session summary"
    SESSION_RANGE:
      end: "[today's date]"
```

This provides:

- Last session events
- Character states at session end
- Open plot threads

### Step 4: Write Day Plan (MANDATORY — NEW)

Before building the scene state, write a **Day Plan** for the fiction day. This drives the session. Without it, NPCs default to reactive observation.

The Day Plan goes in the session file, before the first scene:

```markdown
## Day Plan: Day [N]

### NPC Agendas

| NPC | Goal Today | Move | Clock |
|-----|-----------|------|-------|
| [Name] | [Specific goal] | [Specific action they take] | [When] |

### Collisions

- [Which agendas conflict and where they meet]

### Clocks

- [Timed events that happen regardless of PC action]

### Scene Entry

- First scene opens with: [situation already in progress]
```

**Requirements:**

- Every NPC with a character file gets an agenda
- At least 2 agendas must COLLIDE (create conflicts the PC walks into)
- At least 1 CLOCK must exist (something that happens at a specific time whether or not the PC is present)
- The first scene must open IN THE MIDDLE of something happening — not an empty room waiting for the PC

**The Day Plan is NOT a script.** Player actions alter outcomes. But the pressures exist independently of the player. The building is alive.

### Step 5: Build Initial Scene State

Create or update session file with scene state schema:

```yaml
---
campaign: "<campaign-name>"
run_state: "<the run's state file>#<section>"   # e.g. Work/rp/<run>/<pc>.md#Run state; omit if none
log_dir: "Work/rp/<campaign>/log"   # the day files; omit for a campaign with no fiction day
log_day: <N>                        # a day file's fiction day; rotation writes it on every later file
currentDate: "Day <N> (<weekday>)"  # "Day N" is the fiction day the log rotates on
startDate: "<in-fiction date>"
currentLocation: "<location>"
currentTime: "00:00"
participants: ["<PC>", "<NPC>"]
scene:
  time: "00:00"
  location: "<location>"
  participants:
    - name: "<PC>"
      role: "PC"
    - name: "<NPC>"
      role: "NPC"
      profile_file: "Lore/World/Characters/<NPC>.md"
  mood: "<one-word scene register>"
  pending_rolls: []
  power_holder: null
  power_method: null
run:                                # optional: typed run state; omit to keep run state as free text
  stats:                            # an integer, or "current/max"
    POW: "54/60"
  body:                             # one line of text per part, citing the beat that worked it
    hooves: "feathered draught hooves, shod (T21, B3549)"
  workings:                         # on or off
    SURE-FOOTED: on
  clocks:                           # the fiction day it fires, and what fires
    vine-test: {day: 198, fires: "the vine test at the wall"}
---
```

`run:` is optional. With it, the PC's stats, body facts, standing workings and clocks are typed: `/rp:turn` lints every `run.*` change in a delta and applies it with the beat, and rotation carries the block into each day file. Without it, run state stays free text in the run state file and the header, as before. A continuing session carries the block forward (rotation does it for day files); a new session file copies it from the YAML inside the run state file's generated block. Only the Keeper adds a stat. Deltas change stats, add or change body facts and clocks and clear them, and turn workings on or off; a working is never removed by a delta, and one the Keeper strikes comes out by hand.

`run_state:` names where the run's current state lives (PC stats, workings, standing facts), taken from the `run_state` register in `Memory/canon-layout.md` (a project with several campaigns may name it in each campaign's own table instead). `/rp:turn` reads current state from it and from this file's frontmatter and header, never from `Memory/invariants.md`, which holds static canon. A continuing session carries the previous session's `run_state:` forward. Without the key, the gate falls back to a ``State: `<path>` § <section>`` line in the session header.

`log_dir:` names the campaign's day-file directory, under `Work/rp/` (the campaign's run directory plus `log`, e.g. `Work/rp/garret/log`). Each day file is `<log_dir>/day-NNNN.md`; rotation copies this frontmatter forward, adding `log_day`, `continues` (the file before) and `opened` (the real date it opened). The header between the frontmatter and the first Day Plan (tier, register, GM line, standing facts, live workings and the trigger table, open threads) is copied into each new day file as written, so keep it current there. Without `log_dir`, or without a `Day N` in `currentDate`, the log stays one file per session.

### Step 6: Enforce Scene Pressure Protocol

**HARD RULES:**

1. **Drive scenes with NPC actions.** Do NOT end every beat with "What does Alder do?" Only pause for player input at genuine decision points — moments where the PC must choose between real options.

2. **Spawn agents for critical moments only.** Confrontations, high-stakes interactions, revelations, and first-appearance-in-scene. Routine interactions you write yourself.

3. **When you spawn, include GM_DIRECTIVE.** Every spawn that requires initiative gets a directive: "INITIATE — act FIRST." "GOAL: [specific]." "COST: [what this interaction takes from the PC]."

4. **Scenes start in the middle.** The room was already doing something before the PC arrived. The PC walks into a situation, not an empty room.

5. **One complete beat.** Give each Keeper turn exactly one dramatic beat,
   including the immediate response needed to make its consequence legible.
   Never stop mid-dialogue, and stop before beginning a second beat. Close the
   visible draft with the one-line `Scene: ... | Location: ... | Present: ...`
   anchor required by `/rp:turn` and the roleplay-gm contract.

6. **Every significant NPC interaction costs something visible.** Track what's taken or changed. Show it in subsequent scenes, not in the moment itself.

**You are the REFEREE and the SCENE DRIVER.**

You handle: environment, scene pressure, NPC agendas, routine NPC interactions, PC physical sensation, time advancement, consequence delivery.

You delegate to agents: critical NPC voice moments — confrontations, high-stakes interactions, revelations.

**Failure mode is NOT "writing NPC dialogue without spawning." Failure mode is NPCs who observe, acknowledge, position, and wait. If your NPCs aren't DOING things that FORCE the PC to respond, you have failed.**

### Orchestration Mode

The **roleplay-gm** protocol operates through character agents:

- Spawns character agents for ALL profiled NPC responses
- Spawns world-lookup for proper noun resolution
- Spawns timeline-search for historical context
- Resolves roll outcomes directly from the project's mechanics
- Synthesizes subagent results into seamless narrative
- **Never writes profiled NPC responses directly**

## Multi-Agent Architecture

```
Player Input
    ↓
roleplay-gm (Orchestrator)
    ├── world-lookup (haiku) ← Proper nouns
    ├── timeline-search (haiku) ← Past references
    ├── character voice (sonnet/haiku) ← NPC responses
    └── GM-resolved rolls ← Project mechanics
    ↓
Synthesized Narrative
```

**Character Portrayal Requirements** (research-backed):

1. **Internal Thought Processes**:
   - Character agents include internal thoughts showing reasoning
   - GM synthesizes these into narrative without exposing agent boundaries
   - Format: `*Internal: [Character's analysis]*`

2. **Character Knowledge Boundaries**:
   - World-lookup provides what characters can know
   - Characters refuse questions outside their era and knowledge, as the project canon defines them
   - Rejections are in-character, not meta-explanations

## Critical Marker Persistence

**The `rp-active` marker MUST persist through ALL RP activities**:

✅ Lock persists through:

- Subagent spawning and returns
- Scene transitions
- Meta-instructions like `[switch location]`
- Document lookups via subagents

❌ Lock removal ONLY when:

1. User sends explicit `[End RP session]` or `/rp:end`
2. Session-end hook auto-cleanup

## Ending Sessions

When user sends bracketed end instruction (e.g., `[rp end]`, `[end session]`):

1. Run `/rp:end` command for canon synthesis
2. Remove marker: `rm -f Work/markers/rp-active`
3. Save session file
4. Confirm: "RP session closed."

Or invoke `/rp:end` directly for full canon promotion workflow.

## Arguments

Parse {command_args}:

- Empty → Resume from the project's last session state; if none, ask the Keeper for scene and cast
- Character names (e.g., "halloran and alder") → Set up those characters
- Date (e.g., "<fiction-date>") → Set fiction date
- "load [session]" → Continue specific session
- "end" → Trigger `/rp:end` workflow

ARGUMENTS: {command_args}

---

**Execute**: Create marker, retrieve context via timeline-search, build scene state, deploy roleplay-gm orchestrator.
