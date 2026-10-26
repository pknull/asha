---
name: rp-turn
description: "Process a single RP turn through the continuity gate. The GM drafts; a deterministic lint checks every beat; a model reviewer runs only on flagged or consequential beats (a cheap routine pass, a stronger adjudication before any rewrite); rewrites are capped per beat."
argument-hint: "<keeper turn input as natural language; everything after /rp:turn is the input>"
allowed-tools: ["Task", "Read", "Write", "Edit", "Bash"]
---

# /rp:turn — Validated RP Turn

Wraps a single RP turn with the continuity gate. The Keeper's input goes in; a clean (or surrendered) GM response comes out.

This is the per-turn invocation pattern for guarded RP. Use during active sessions instead of sending free input directly to the GM.

The gate is sized to the beat. A deterministic lint (`plugins/rp/tools/rp_gate.py`, no model call, tens of milliseconds) runs on every draft. The model reviewer runs only when the lint flags something or the beat is consequential, and its routine pass uses the cheap model; the stronger model is spent only to adjudicate a violation before a rewrite. Every gate step appends one line to the validator log.

## Project layout assumed

This command uses the conventional RP layout. A project adopting it supplies:

| Path | Role |
|---|---|
| `Work/markers/rp-active` | session marker; `/rp:start` records the live session file in it |
| `Memory/invariants.md` | the compiled invariants projection the gate validates against: static canon only |
| `Work/rp/<campaign>/log/day-NNNN.md` | the live session record, one file per fiction day (resolved through the marker, never the date); its frontmatter and header carry current state. A legacy `Work/rp/rp_session_*.md` log works too, and rotates into day files once its frontmatter names a `log_dir` |
| the run's state file | named by the session: `run_state: "<path>#<section>"` in its frontmatter, else a ``State: `<path>` § <section>`` line in its header |
| `Work/rp/_validator_log.jsonl` | per-turn telemetry: one line per gate step |
| `Work/rp/.gate/turn.md` | scratch: the current GM turn, overwritten on each attempt (and `beat.md`, the text `ship` appends; `day-summary.md`, a closed day's summary before it is published) |
| `Work/rp/.gate/struck/` | drafts the gate struck, kept until `rotate` writes them into the log (see **Struck drafts**) |
| `Work/rp/summaries/<campaign>/day-NNNN.md` | one summary per fiction day, published once after rotation closes the day |
| `.claude/modes/rp.yaml` | optional: the `turnGate` block (policy, rewrite caps, models; see the plugin README) |

## Preconditions

1. RP session must be active: `Work/markers/rp-active` must exist
2. Invariants document must exist: `Memory/invariants.md` (run `/rp:extract-invariants` first if not)
3. A session file must exist (`/rp:start` creates it)

## Protocol

### Step 1: Verify preconditions and resolve the session file

```bash
test -f Work/markers/rp-active || echo "ERROR: No active RP session. Run /rp:start first."
test -f Memory/invariants.md || echo "ERROR: No invariants. Run /rp:extract-invariants first."
ASHA_ROOT="${ASHA_ROOT:-$(jq -r '.asha_root // empty' "${ASHA_HOME:-$HOME/.asha}/config.json" 2>/dev/null)}"; python3 "$ASHA_ROOT/plugins/rp/tools/rp_gate.py" session --project-dir .
```

Every gate command in this file resolves `ASHA_ROOT` on its own line, because shell variables do not survive between tool calls; if it is unresolved the command fails with a missing `/plugins/rp/tools/rp_gate.py`: run `./install.sh` or launch through the `asha` wrapper.

The `session` command prints `{"path", "source", "warning", "gate", "live_state", "invariants"}`. Write `path` literally wherever this file says `<SESSION_FILE>`, and take GM_MODEL from `gate.models.gm`. A session can run for days, so the date never names it:

- `source: marker`: the marker names it. Proceed.
- `source: latest`: the marker names no (existing) session file, so the newest open session is used: the head of a campaign's day files (the one with no rotation footer), or the newest legacy `Work/rp/rp_session_*.md` not marked `status: closed`. Tell the Keeper the warning once, in one line, and proceed. `/rp:start` records the file in the marker.
- `source: new`: no open session file is found; the warning says why, and names any existing file to `load`. Halt and ask the Keeper to run `/rp:start`.

`live_state` says where current state is, as line ranges: `frontmatter` and `header` in SESSION_FILE (the header runs to the first Day Plan or beat heading), and `run_state` (the run's state file, its section, and `current`, the section's first entry). Its `warnings` say what could not be resolved; with no `run_state`, current state comes from the session alone.

`continuity` is the continuity read, as line ranges: the last `beats` beats (`sessionLog.previousBeats` in the mode file, default 20), wherever they fall, and never the whole day once it holds more. `current` is its part in SESSION_FILE: the day's last `beats` beats (`whole_day: false`), or, while the day holds fewer, the day so far with its Day Plan (`whole_day: true`; in a legacy log the day starts at its first Day Plan or beat). `previous` is a list, oldest first, that tops the read up to `beats` from before the day: a legacy log's earlier beats, then the files before it, a day file's `continues:` first. `plans` lists, in order, every Day Plan section of the current day that the `day` range does not hold (the first plan, an ADDENDUM, a second session's plan, or one written early before an earlier day's last beat, in this file or the file before), since a long day's window no longer reaches them; it is empty when the `day` range holds them all or the day has none. `current` is null and `previous` empty when there is nothing to read; `continuity` is null when the session has no fiction day. Every range leaves struck drafts out: an entry with one inside carries `parts`, the pieces to read. A `rotation_warning` means an earlier rotation stopped before closing the old file; Step 3e's `rotate` closes it, so do not tell the Keeper.

`invariants.warnings` lists `live_state` (the invariants still carry a snapshot of state, with its lines), `stale` (`last_extraction` predates the run's state) or `no_extraction_date`. Tell the Keeper each once per session, in one line, with the fix (`/rp:extract-invariants`), and proceed: the gate reads current state from LIVE_STATE either way, and the reviewer ignores any snapshot in the invariants.

`run` reports typed run state: `schema` is true when the session frontmatter carries a `run:` block (stats, body facts, workings on or off, clocks; see the plugin README). Its `warnings` are yours, not the Keeper's: a malformed entry is fixed by hand between turns (as a `log_dir` line is added), and a hand-written current entry in the run state file goes back below the block at the next `state --refresh`. `notes` lists clocks past their day that no delta has fired or cleared (`key`, `day`): fire each in the fiction when it is due, then clear it (`<key>: null`) or move it in a delta. `check` reports the same as `lint.run_notes`. `state_block` says whether the run state file's generated copy of the block is behind the fiction day, and `summaries_due` lists closed days just behind the session with no summary yet, including one `/rp:start` closed (`summary`, run by Step 3e and `/rp:end`, walks the whole run and writes every missing one).

If any precondition fails, halt and inform the Keeper. Do not proceed.

### Step 2: Read current scene state

Read SESSION_FILE's YAML frontmatter, its header and its current `## Day Plan` sections (`continuity.plans`, or inside the `day` range), not the whole file: a long session runs to megabytes. Read the run state's `current` entry too. Take every range from `live_state`.

- Current scene state YAML (location, time, present, observable)
- Standing facts, stats and what is on the PC (the header and the run state's current entry)
- Recent register-stack installations (to pass to the gate)
- Day Plan (needed by the `missing_day_plan` check)

Construct the SCENE_STATE block from these, and the LIVE_STATE block that tells the GM and the reviewer where to read current state:

```
LIVE_STATE:
  session: <SESSION_FILE> lines <frontmatter.start>-<header.end>
  run_state: <run_state.path> lines <current.start>-<current.end>  (the section: lines <heading_line>-<end_line>)
  day: <continuity.current.path> lines <start>-<end>  (Day <continuity.day>: so far | its last <beats> beats; B<first_beat>-B<last_beat>)
  earlier: <path> lines <start>-<end>  (<beats> beats, B<first_beat>-B<last_beat>)   # one line per continuity.previous entry, oldest first
  plan: <path> lines <start>-<end>  (<heading>)   # one line per continuity.plans entry, in order
```

When `header` is null (the Day Plan follows the frontmatter), the session range ends at `frontmatter.end`; when `header` has `parts`, or `header.start` is past `frontmatter.end + 1` (a struck draft sits in the header region), write the session range as `<frontmatter.start>-<frontmatter.end>` and then the header's parts, or `<header.start>-<header.end>`; when `header.truncated` is true, add "(header cut at 250 lines)". When `current` is null (the section opens with a subheading), give the section's range instead, marked "search it; do not read it whole". Omit the `run_state` line when `live_state.run_state` is null or `generated` (that entry is the copy of the frontmatter's `run:` block, which the session range already holds), the `day` line when `continuity.current` is null, and the `earlier` lines when `continuity.previous` is empty. Write "so far" when `whole_day` is true, else "its last <beats> beats"; omit the beat span when `first_beat` is null (a day with only its Day Plan). When an entry has `parts`, write its lines as those parts (`lines 1000-1180, 1210-1380`): the lines between are struck drafts, never read. Write a `plan` line for each `continuity.plans` entry (none when the list is empty: the `day` range holds the plan). The reader reads these ranges, never the whole day. Current state is never read from `Memory/invariants.md`, which holds static canon: where the two disagree (a death, a departure, a custom no longer kept), LIVE_STATE governs the run.

### Step 3: Attempt loop

Each `check` result carries a `route` object: `next`, `beat` (routine or consequential), `max_rewrites` (by default 1 for a routine beat, 3 for a consequential one), `reviewer_model` and `adjudicator_model`. MAX_REWRITES is the largest `route.max_rewrites` any check of this turn has reported, so a beat that turns out consequential keeps its larger cap.

```
ATTEMPT_NUMBER = 1
REWRITES = 0
PRIOR_VIOLATIONS = null
ESCALATED = false      # once the adjudicator confirms a violation, it reviews the rewrites
GM_MODEL = gate.models.gm from Step 1
MAX_REWRITES = 0       # raised to the largest route.max_rewrites seen this turn

LOOP:

  # 3a. Spawn roleplay-gm to draft narration
  Spawn roleplay-gm via Task:
    subagent_type: "roleplay-gm"
    model: <GM_MODEL>
    prompt: |
      KEEPER_INPUT: <the {command_args} from /rp:turn>
      SESSION_FILE: <SESSION_FILE>
      SCENE_STATE: <constructed from session file>
      LIVE_STATE: <the block from Step 2>
      ATTEMPT_NUMBER: <ATTEMPT_NUMBER>
      PRIOR_VIOLATIONS: <PRIOR_VIOLATIONS if attempt > 1>

      Current state (who is alive and where, the day, stats, what is on
      the PC, standing facts) comes from LIVE_STATE and SCENE_STATE, never
      from Memory/invariants.md, which holds static canon.

      Draft the GM response for this turn. If PRIOR_VIOLATIONS is present,
      the previous draft failed validation. Use the suggested_fix entries
      to address each violation in your rewrite.

      WALKTHROUGH CONTRACT: Return exactly one complete dramatic beat for this
      turn. Never stop mid-dialogue: finish the immediate exchange or action
      that gives the beat its consequence, then stop before beginning a second
      beat. End the visible draft with exactly one anchor line:
      Scene: <scene> | Location: <location> | Present: <comma-separated names>

      Respect the protocol (Day Plan, character agents for profiled NPCs,
      GM_DIRECTIVE on spawns, register-stack persistence, no softening,
      no time skips, no fade-to-black).

      Emit GM_SPAWN_LOG, SOURCE_LOG (with priced_stake_touched,
      canon_proposed and pc_changed) and SCENE_STATE_DELTA (changed
      frontmatter keys only — time, location, participants, power, and
      run.* when the session keeps a run: block) with the draft. You
      cannot write files; the delta is how state changes reach the
      session file.

  Receive DRAFT, GM_SPAWN_LOG, SOURCE_LOG and SCENE_STATE_DELTA from roleplay-gm output.

  # 3b. Lint the draft (every beat; no model call)
  Write Work/rp/.gate/turn.md with five headed blocks, each verbatim:
    KEEPER_INPUT:, DRAFT:, GM_SPAWN_LOG:, SOURCE_LOG:, SCENE_STATE_DELTA:
  Run (add --beat <label> when the session numbers its beats; any
  --attempt above 1 is treated as a rewrite and always reviewed):
    ASHA_ROOT="${ASHA_ROOT:-$(jq -r '.asha_root // empty' "${ASHA_HOME:-$HOME/.asha}/config.json" 2>/dev/null)}"; python3 "$ASHA_ROOT/plugins/rp/tools/rp_gate.py" check --project-dir . --session-file "<SESSION_FILE>" --turn-file Work/rp/.gate/turn.md --attempt <ATTEMPT_NUMBER>
  The check appends its own `lint` line (and a `skipped` line when no review
  runs) to the validator log. It reports lint.findings (each with fix: edit or
  review), triggers (why the beat is consequential), scene_warning (the
  frontmatter could not be read), invariants (Step 1's warnings again, which
  the lint line records as invariants_warnings; do not repeat them to the
  Keeper) and route. Raise MAX_REWRITES to route.max_rewrites if it is larger.

  route.next:
    fix    → the findings are mechanical (anchor missing, duplicated or not
             last; unknown or root-mirror delta keys; delta disagreeing with
             the anchor's Present list or time). Correct the anchor or the
             delta to match the prose; never edit the prose to fit them, and
             never drop someone the prose keeps on the page. Rewrite the turn
             file and run the same check with --recheck: a recheck answers
             ship or review, never fix again. No model call.
    ship   → go to 3e. (A check of attempt 2 or later never answers ship.)
    review → go to 3c.

  # 3c. Review: the routine pass (cheap model)
  Spawn continuity-reviewer via Task:
    subagent_type: "continuity-reviewer"
    model: <route.adjudicator_model if ESCALATED else route.reviewer_model>
    prompt: |
      MODE: live_roleplay
      DRAFT: <DRAFT>
      KEEPER_INPUT: <the Keeper's input; read only to bound pc_authored>
      INVARIANTS_FILE: "Memory/invariants.md"
      SESSION_FILE: <SESSION_FILE>  (read its frontmatter, header and current Day Plan, and the LIVE_STATE day and earlier ranges, never the whole day)
      SCENE_STATE: <constructed scene state>
      LIVE_STATE: <the block from Step 2>  # current state; never from the invariants
      ATTEMPT_NUMBER: <ATTEMPT_NUMBER>
      REWRITES_LEFT: <MAX_REWRITES - REWRITES>
      PRIOR_VIOLATIONS: <PRIOR_VIOLATIONS if attempt > 1>
      GM_SPAWN_LOG: <GM_SPAWN_LOG from roleplay-gm>
      SOURCE_LOG: <SOURCE_LOG from roleplay-gm>
      SCENE_STATE_DELTA: <SCENE_STATE_DELTA from roleplay-gm>
      LINT: <lint.findings and triggers from the check>
      # The delta rides THROUGH the gate, not around it: the reviewer checks
      # every delta entry against the prose (scene_state_mismatch) because a
      # clean verdict commits this delta to the frontmatter.
      PRICED_STAKES: <the `priced_stakes` path from Memory/canon-layout.md;
        default Lore/TTRPG/canon-sources.md when no register exists>
      # Resolve from the register, not a literal: a project keeping its
      # stake register elsewhere would otherwise be validated against a
      # nonexistent file — silently, because a missing source and an
      # unpriced stake look identical to the reviewer.

  Receive VERDICT (clean | violations_found | hard_fail).
  Log it:
    ASHA_ROOT="${ASHA_ROOT:-$(jq -r '.asha_root // empty' "${ASHA_HOME:-$HOME/.asha}/config.json" 2>/dev/null)}"; python3 "$ASHA_ROOT/plugins/rp/tools/rp_gate.py" log --project-dir . --session-file "<SESSION_FILE>" --path review --model <model used> --attempt <ATTEMPT_NUMBER> --verdict <verdict> --violations <categories,comma,separated> --clean <categories>

  if clean → 3e.
  if every violation is soft → apply each suggested_fix yourself (the edit
     pass; no model call), rewrite the turn file, run check with --final, and
     go to 3e. On --final, route.next is ship, or fix for a mechanical slip
     the edit introduced: correct it and run --final once more. If the second
     --final still says fix, ship and name the remaining findings in the turn
     line.
  if any violation is hard, or hard_fail:
     if ESCALATED → the adjudicator has reviewed this rewrite: treat its
        verdict as confirmed and go to 3d's decision.
     else → 3d.

  # 3d. Adjudicate before any rewrite (stronger model)
  Spawn continuity-reviewer via Task with model: route.adjudicator_model and
  the same prompt, plus:
      ADJUDICATE: <the routine pass's violations, verbatim>
  It confirms or overturns each violation and returns a verdict over the
  confirmed ones only. Log it:
    ASHA_ROOT="${ASHA_ROOT:-$(jq -r '.asha_root // empty' "${ASHA_HOME:-$HOME/.asha}/config.json" 2>/dev/null)}"; python3 "$ASHA_ROOT/plugins/rp/tools/rp_gate.py" log --project-dir . --session-file "<SESSION_FILE>" --path adjudicate --model <route.adjudicator_model> --attempt <ATTEMPT_NUMBER> --verdict <verdict> --violations <confirmed categories>

  Decision:
    clean (all overturned) or soft only → apply any soft fixes in the edit
      pass, run check with --final, go to 3e.
    confirmed hard, not hard_fail, and REWRITES < MAX_REWRITES →
      STRIKE the draft the rewrite replaces, before the GM is spawned again
      (the rewrite's 3b overwrites the turn file; drop --beat only when the
      session numbers no beats):
        ASHA_ROOT="${ASHA_ROOT:-$(jq -r '.asha_root // empty' "${ASHA_HOME:-$HOME/.asha}/config.json" 2>/dev/null)}"; python3 "$ASHA_ROOT/plugins/rp/tools/rp_gate.py" strike --project-dir . --session-file "<SESSION_FILE>" --turn-file Work/rp/.gate/turn.md --attempt <ATTEMPT_NUMBER> --beat <label> --reason "<confirmed categories>"
      ESCALATED = true; PRIOR_VIOLATIONS = the confirmed violations;
      REWRITES += 1; ATTEMPT_NUMBER += 1; continue LOOP at 3a.
    otherwise (hard_fail, or no rewrites left) → 3f.

  # 3e. Ship
  ROTATE first: the beat's fiction day picks its file (one file per day).
  Work/rp/.gate/turn.md holds the turn as it ships (after any edit pass).
  Run (add --beat <label> when the session numbers its beats):
    ASHA_ROOT="${ASHA_ROOT:-$(jq -r '.asha_root // empty' "${ASHA_HOME:-$HOME/.asha}/config.json" 2>/dev/null)}"; python3 "$ASHA_ROOT/plugins/rp/tools/rp_gate.py" rotate --project-dir . --session-file "<SESSION_FILE>" --turn-file Work/rp/.gate/turn.md
  It prints {"action", "path", "previous", "day", "flag", "struck",
  "warnings"}. From here on SESSION_FILE is `path`, for the append, the delta
  and the turn line. `struck` names the struck drafts now in `path` (this
  turn's and any a crash left waiting), in place before the beat.
  The beat heading you write carries `day` (Day <day>).
    rotated   → the day moved on: `previous` now ends with a footer pointing
                to `path`, which opens with the header copied as written.
                Never edit `previous` again. The GM's Day Plan for the new
                day goes first in the shipped text, then the beat.
    step_back → the beat's day is earlier than the file's (a flashback or a
                numbering slip): it stays in this file. Append `flag` (already
                in parentheses) at the very end of the beat heading, after the
                heading's own parentheses. Rotation never moves back.
    none      → same day, or no fiction day: carry on.
    refused   → (exit 1) nothing was written. Either a day file of that
                name exists and is not this run's, or the day jumps more
                than 7 days (a typo would split the log past every day
                between, for good): stop and show the Keeper the warning. Only
                on the Keeper's word that the jump is real, run rotate again
                with --allow-jump.
  The day comes from the gated delta, never from a heading. A failed rotate
  is safe to run again as is.
  SHIP: write Work/rp/.gate/beat.md with the text the log gets (the new
  day's Day Plan first when rotate said rotated, then the beat heading, then
  DRAFT), and run (add --beat <label> when the session numbers its beats):
    ASHA_ROOT="${ASHA_ROOT:-$(jq -r '.asha_root // empty' "${ASHA_HOME:-$HOME/.asha}/config.json" 2>/dev/null)}"; python3 "$ASHA_ROOT/plugins/rp/tools/rp_gate.py" ship --project-dir . --session-file "<SESSION_FILE>" --turn-file Work/rp/.gate/turn.md --append-file Work/rp/.gate/beat.md
  ship appends the text and applies SCENE_STATE_DELTA to the frontmatter in
  one write, so neither lands without the other, and it refuses a closed
  file. State rides the same gate as prose: a delta is applied only once
  its draft clears the gate, so rejected drafts leave no phantom state;
  their text is kept only as struck drafts.
  Merge semantics: scene.* is the canonical namespace, and ship syncs the
  root mirrors from it (currentTime := scene.time, currentLocation :=
  scene.location, participants := scene.participants names); run.* changes
  the run: block. A run.* entry, an unknown key or a root mirror the lint
  would still flag is not applied: ship lists it under `dropped`; name it in
  the turn line. A ship re-run after a crash ships once. On this path never
  append the beat or edit the frontmatter by hand.
  Log the turn (Step 4) and OUTPUT DRAFT to Keeper.
  Only when rotate said rotated, after the output (the Keeper waits on
  neither):
    1. Refresh the state block (no model call):
       ASHA_ROOT="${ASHA_ROOT:-$(jq -r '.asha_root // empty' "${ASHA_HOME:-$HOME/.asha}/config.json" 2>/dev/null)}"; python3 "$ASHA_ROOT/plugins/rp/tools/rp_gate.py" state --refresh --project-dir .
       It copies the frontmatter's run: block into the run state file's
       generated block, keeping every hand-written line; without run: it does
       nothing. refused means the block was edited by hand: tell the Keeper
       once, and go on.
    2. Summarise the closed day (one model call per fiction day). Run
       `rp_gate.py summary --project-dir .`; take each `days` entry that does
       not `exist` (rotate's `previous`, and any earlier day still due). Spawn
       timeline-search (model haiku, in the background where the harness
       allows) with QUERY "DAY SUMMARY: Day <day>" and the entry's `beats`
       and `plans` ranges, each as its `parts` when it has them (the lines
       between are struck drafts). Write the text it returns to
       Work/rp/.gate/day-summary.md, then run
       `rp_gate.py summary --project-dir . --day <day> --publish Work/rp/.gate/day-summary.md`.
       A refusal names what to fix (a line cited in a file still open or in a
       struck draft, a beat after the day): fix the text yourself and publish
       again, never with a second model call. A day summary is published
       once, never overwritten.
  BREAK.

  # 3f. Surrender
  STRIKE the final draft: the gate rejected it, so it is kept as a record,
  never as story (drop --beat only when the session numbers no beats):
    ASHA_ROOT="${ASHA_ROOT:-$(jq -r '.asha_root // empty' "${ASHA_HOME:-$HOME/.asha}/config.json" 2>/dev/null)}"; python3 "$ASHA_ROOT/plugins/rp/tools/rp_gate.py" strike --project-dir . --session-file "<SESSION_FILE>" --turn-file Work/rp/.gate/turn.md --attempt <ATTEMPT_NUMBER> --beat <label> --reason "surrendered: <categories>"
  Nothing is appended to SESSION_FILE now: the next rotate (3e of the beat
  that ships, or /rp:end) writes the turn's struck drafts into the file that
  beat goes to.
  Log the turn (Step 4) with verdict surrendered.
  OUTPUT DRAFT to Keeper plus violations report plus prompt:
    "Validator surrendered after <ATTEMPT_NUMBER> attempts. Draft above with violations.
    Options: (1) accept-anyway and continue, (2) provide direction and retry,
    (3) manually edit the draft and tell me to ship the edited version."

  Log the Keeper's choice as a second turn line (--verdict keeper_accepted,
  keeper_edited or keeper_retry; a retry then starts a new turn).
  On (1) accept-anyway: the Keeper's acceptance IS the gate decision for
  this draft. Ship it through 3e as it stands, "KEEPER: accepted after
  surrender" in its beat heading: rotate, write Work/rp/.gate/beat.md as 3e
  does (the new day's Day Plan first when rotate said rotated, then the beat
  heading, then the draft), and ship it, which appends it and applies its
  SCENE_STATE_DELTA in one write (skipping the delta reintroduces the
  stale-frontmatter bug through the surrender door); then 3e's refresh and
  summary if rotate said rotated. The struck copy stays as the record of
  what the gate rejected; only the shipped beat is story.
  On (2): the new turn's rejected drafts are struck in turn, and all of them
  reach the file its beat goes to.
  On (3): put the Keeper's edit in Work/rp/.gate/turn.md, revising the
  delta first if the edit changed the state facts, and ship it through 3e.
  BREAK.

END LOOP
```

`[validate]` anywhere in the Keeper's input makes the beat consequential, so it is always reviewed, even under `turnGate.review: lint-only`.

### Step 4: Log the turn

Every gate step appends one line to `Work/rp/_validator_log.jsonl`: `check` writes `lint` (and `skipped` when no review runs); the command writes `review`, `adjudicate` and, once per turn, `turn`:

```bash
ASHA_ROOT="${ASHA_ROOT:-$(jq -r '.asha_root // empty' "${ASHA_HOME:-$HOME/.asha}/config.json" 2>/dev/null)}"; python3 "$ASHA_ROOT/plugins/rp/tools/rp_gate.py" log --project-dir . --session-file "<SESSION_FILE>" --path turn --attempts <ATTEMPT_NUMBER> --verdict <clean|soft_fixed|surrendered|unreviewed> --violations <categories left, if any>
```

Pass the same `--beat <label>` on every line of a turn when the session numbers its beats, so the lines group by beat. A beat that reached the Keeper without a `turn` line did not pass through this gate. When a gate step itself fails (the check errors, a reviewer spawn fails) and the Keeper ships the draft anyway, log the turn with `--verdict unreviewed --note "<what failed>"`.

### Step 5: Marker persistence

The `Work/markers/rp-active` marker MUST persist through all subagent spawns and the entire turn loop. Never remove it during `/rp:turn` execution.

## Output to Keeper

On a clean pass (most turns): the GM-drafted prose for one complete beat followed
by its single `Scene: ... | Location: ... | Present: ...` anchor line, as if
straight from the orchestrator.

On surrender: the draft + the violations report + the three options above.

Do not narrate the validation process to the Keeper unless they ask. The gate runs invisibly when it passes; it only surfaces when it surrenders, or once when the session file came from the fallback.

## Failure Modes

| Failure | Handling |
|---|---|
| `Memory/invariants.md` missing | Halt, instruct Keeper to run `/rp:extract-invariants` |
| `Work/markers/rp-active` missing | Halt, instruct Keeper to run `/rp:start` first |
| `invariants.warnings` (`live_state`, `stale`, `no_extraction_date`) | Tell the Keeper once per session with the fix (`/rp:extract-invariants`); proceed |
| `live_state` warning (no run state resolved) | Proceed on the session's frontmatter and header; tell the Keeper once |
| No open session file (`source: new`) | Halt, instruct Keeper to run `/rp:start` |
| roleplay-gm spawn fails | Surface error to Keeper; do not retry blindly |
| `rp_gate.py check` fails | Surface the error; offer to review the draft with the routine pass instead (Keeper's choice); log the turn with `--note` |
| continuity-reviewer spawn fails | Surface error and offer to ship the draft unvalidated (Keeper's choice); log the turn with verdict `unreviewed` |
| No rewrites left | Surrender flow as above |
| `rp_gate.py rotate` fails | Surface the error and run the same command again: every step is safe to repeat. Do not append the beat until it names a file |
| `rotate` warns about struck drafts (an entry it cannot read or write, or one whose session file is gone) | Ship the beat anyway: a struck draft never holds one back. Tell the Keeper once, naming the stash file; it stays in `Work/rp/.gate/struck/` |
| `rp_gate.py strike` fails | Run the same command again (a kept draft is never kept twice). If it still fails, tell the Keeper once that the draft was not kept, and go on: a struck draft is a record, never story |
| `rotate` answers `refused` | Halt; show the Keeper the warning (a day file of that name belongs to another run or is closed, or the day jumps more than 7 days; for a real jump the Keeper confirms and `rotate` runs again with `--allow-jump`) |
| `rp_gate.py ship` fails | Surface the error and run the same command again: a retry ships once. An `ERROR` (a closed file, a malformed delta) wrote nothing: fix what it names. Never append the beat by hand |
| `state --refresh` answers `refused` | The block was edited by hand, or the run state pointer names a log or the invariants: tell the Keeper once; play goes on (the frontmatter governs) |
| The day summary fails | Play goes on; `summaries_due` keeps the day, and `/rp:end` writes it |

## Arguments

`{command_args}` is the Keeper's free-text input for this turn — what their PC says, does, or instructs. Pass it verbatim as KEEPER_INPUT to roleplay-gm.

ARGUMENTS: {command_args}

## Notes

- This command does NOT replace `/rp:start`. `/rp:start` initializes a session; `/rp:turn` runs each turn within an active session.
- The Keeper can mix `/rp:turn` and ungated input: anything sent without `/rp:turn` goes straight to the GM unvalidated and leaves no gate line, and never rotates the log.
- One log file per fiction day. A beat belongs to the day it ends on, so a night beat that wakes on the next morning opens the next day's file, after that day's Day Plan. The beat's day is the delta's `currentDate` (the lint holds it to the anchor's `Scene: Day N`), else the frontmatter's; a campaign whose frontmatter carries no `Day N`, or names no `log_dir`, keeps one file as before. `sessionLog.rotation: off` in the mode file turns rotation off.
- A beat is **consequential** when the check reports a trigger: `priced_stake` (SOURCE_LOG, or the priced-stakes phrases in the Keeper's input), `canon_proposed`, `pc_change` (SOURCE_LOG `pc_changed`, or a new PC role in the delta), `arrival_departure` (the Present list changed, or someone arrives on the page), `fight_or_capture`, `validate_tag`, or a project `consequentialPatterns` match. Anything else is **routine**.
- Model calls per beat, excluding the character agents the GM spawns:

  | Beat | Before (0.3.x: review every beat) | Now (defaults) |
  |---|---|---|
  | Routine, lint clean | 2 sonnet | 1 sonnet (the GM) |
  | Routine flagged by the lint, or consequential; review clean or soft only | 2 sonnet (soft findings forced a rewrite: 4) | 1 sonnet + 1 haiku |
  | One confirmed hard violation, fixed by one rewrite | 4 sonnet | 4 sonnet + 1 haiku (GM, routine pass, adjudication, rewrite, re-review) |
  | Worst case, routine (1 rewrite) | 8 sonnet | 4 sonnet + 1 haiku |
  | Worst case, consequential (3 rewrites) | 8 sonnet | 8 sonnet + 1 haiku |

- Run state with a `run:` block adds no model call per beat: its entries are linted (a known field, the right type, a working only on or off, a clock's day from the beat's day to a year ahead), a finding the edit pass leaves never sends a beat to the reviewer, and the reviewer sees `run.*` entries only in the deltas of beats already routed to it. Once per fiction day, after the beat that closed the day is shown, one haiku call writes that day's summary.
- The table assumes the harness honours a per-spawn `model`, as Claude Code's Task tool does. Codex, Copilot and OpenCode were not checked here: where a spawn ignores it, the routine pass and the adjudicator run on the same model and adjudication buys a second opinion rather than a cheaper first one.
- `turnGate.review: every` restores review on every beat; `lint-only` spends a model call on review only for `[validate]`.

## Struck drafts

A draft the gate rejects is kept as a record, never as story: each draft a rewrite replaces (3d), and a surrender's final draft (3f). `strike` stashes the turn file's DRAFT, and `rotate` writes it into the file the beat goes to, before the beat, as a folded callout:

```markdown
> [!struck]- STRUCK, not canon: Beat 4174, attempt 1, struck 2026-10-26. Reason: presence, scene_state_mismatch
>
> <the draft, line by line, as the gate checked it>
>
> <!-- rp-struck: 3f9c0a1b2c3d4e5f -->
```

No model call and nothing extra in any read: the continuity ranges leave it out (`parts`), it is no beat, header or Day Plan, and ratification, the summary and canon exclude it. The edit pass and the mechanical fixes amend a draft rather than reject it, so they strike nothing. A beat the Keeper strikes in chat after it shipped has no path here: it stays in the log as played.
