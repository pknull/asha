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
| `Work/rp/rp_session_*.md` | the live session record (resolved through the marker, never the date); its frontmatter and header carry current state |
| the run's state file | named by the session: `run_state: "<path>#<section>"` in its frontmatter, else a ``State: `<path>` § <section>`` line in its header |
| `Work/rp/_validator_log.jsonl` | per-turn telemetry: one line per gate step |
| `Work/rp/.gate/turn.md` | scratch: the current GM turn, overwritten on each attempt |
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
- `source: latest`: the marker names no (existing) session file, so the newest open `Work/rp/rp_session_*.md` is used. Tell the Keeper the warning once, in one line, and proceed. `/rp:start` records the file in the marker.
- `source: new`: no session file exists. Halt and ask the Keeper to run `/rp:start`.

`live_state` says where current state is, as line ranges: `frontmatter` and `header` in SESSION_FILE (the header runs to the first Day Plan or beat heading), and `run_state` (the run's state file, its section, and `current`, the section's first entry). Its `warnings` say what could not be resolved; with no `run_state`, current state comes from the session alone.

`invariants.warnings` lists `live_state` (the invariants still carry a snapshot of state, with its lines), `stale` (`last_extraction` predates the run's state) or `no_extraction_date`. Tell the Keeper each once per session, in one line, with the fix (`/rp:extract-invariants`), and proceed: the gate reads current state from LIVE_STATE either way, and the reviewer ignores any snapshot in the invariants.

If any precondition fails, halt and inform the Keeper. Do not proceed.

### Step 2: Read current scene state

Read SESSION_FILE's YAML frontmatter, its header and its current `## Day Plan` section, not the whole file: a long session runs to megabytes. Read the run state's `current` entry too. Take every range from `live_state`.

- Current scene state YAML (location, time, present, observable)
- Standing facts, stats and what is on the PC (the header and the run state's current entry)
- Recent register-stack installations (to pass to the gate)
- Day Plan (needed by the `missing_day_plan` check)

Construct the SCENE_STATE block from these, and the LIVE_STATE block that tells the GM and the reviewer where to read current state:

```
LIVE_STATE:
  session: <SESSION_FILE> lines <frontmatter.start>-<header.end>
  run_state: <run_state.path> lines <current.start>-<current.end>  (the section: lines <heading_line>-<end_line>)
```

When `header` is null (the Day Plan follows the frontmatter), the session range ends at `frontmatter.end`; when `header.truncated` is true, add "(header cut at 250 lines)". When `current` is null (the section opens with a subheading), give the section's range instead, marked "search it; do not read it whole". Omit the `run_state` line when `live_state.run_state` is null. Current state is never read from `Memory/invariants.md`, which holds static canon: where the two disagree (a death, a departure, a custom no longer kept), LIVE_STATE governs the run.

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
      frontmatter keys only — time, location, participants, power) with
      the draft. You cannot write files; the delta is how state changes
      reach the session file.

  Receive DRAFT, GM_SPAWN_LOG, SOURCE_LOG and SCENE_STATE_DELTA from roleplay-gm output.

  # 3b. Lint the draft (every beat; no model call)
  Write Work/rp/.gate/turn.md with five headed blocks, each verbatim:
    KEEPER_INPUT:, DRAFT:, GM_SPAWN_LOG:, SOURCE_LOG:, SCENE_STATE_DELTA:
  Run (add --beat <label> when the session numbers its beats; any
  --attempt above 1 is treated as a rewrite and always reviewed):
    ASHA_ROOT="${ASHA_ROOT:-$(jq -r '.asha_root // empty' "${ASHA_HOME:-$HOME/.asha}/config.json" 2>/dev/null)}"; python3 "$ASHA_ROOT/plugins/rp/tools/rp_gate.py" check --project-dir . --session-file <SESSION_FILE> --turn-file Work/rp/.gate/turn.md --attempt <ATTEMPT_NUMBER>
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
      SESSION_FILE: <SESSION_FILE>  (read its frontmatter, header and current Day Plan only)
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
    ASHA_ROOT="${ASHA_ROOT:-$(jq -r '.asha_root // empty' "${ASHA_HOME:-$HOME/.asha}/config.json" 2>/dev/null)}"; python3 "$ASHA_ROOT/plugins/rp/tools/rp_gate.py" log --project-dir . --session-file <SESSION_FILE> --path review --model <model used> --attempt <ATTEMPT_NUMBER> --verdict <verdict> --violations <categories,comma,separated> --clean <categories>

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
    ASHA_ROOT="${ASHA_ROOT:-$(jq -r '.asha_root // empty' "${ASHA_HOME:-$HOME/.asha}/config.json" 2>/dev/null)}"; python3 "$ASHA_ROOT/plugins/rp/tools/rp_gate.py" log --project-dir . --session-file <SESSION_FILE> --path adjudicate --model <route.adjudicator_model> --attempt <ATTEMPT_NUMBER> --verdict <verdict> --violations <confirmed categories>

  Decision:
    clean (all overturned) or soft only → apply any soft fixes in the edit
      pass, run check with --final, go to 3e.
    confirmed hard, not hard_fail, and REWRITES < MAX_REWRITES →
      ESCALATED = true; PRIOR_VIOLATIONS = the confirmed violations;
      REWRITES += 1; ATTEMPT_NUMBER += 1; continue LOOP at 3a.
    otherwise (hard_fail, or no rewrites left) → 3f.

  # 3e. Ship
  APPEND DRAFT to SESSION_FILE
  APPLY SCENE_STATE_DELTA to SESSION_FILE's YAML frontmatter
    (state rides the same gate as prose: a delta is applied only once the
    draft clears the gate, so rejected drafts leave no phantom state behind.
    Without this step the frontmatter goes stale and every later turn's
    SCENE_STATE and lint are built from the wrong scene.)
    Merge semantics: delta keys are schema dot-paths with scene.* as the
    canonical namespace (scene.time, scene.location, scene.participants,
    scene.mood, scene.power_holder, ...). After applying scene.*, sync the
    root mirrors from it: currentTime := scene.time, currentLocation :=
    scene.location, participants := scene.participants names. The GM never
    emits the root keys directly — one namespace, one merge direction, or
    the mirrors drift apart.
  Log the turn (Step 4) and OUTPUT DRAFT to Keeper. BREAK.

  # 3f. Surrender
  APPEND DRAFT + violations report to SESSION_FILE under a "## VALIDATOR SURRENDER" block
  Log the turn (Step 4) with verdict surrendered.
  OUTPUT DRAFT to Keeper plus violations report plus prompt:
    "Validator surrendered after <ATTEMPT_NUMBER> attempts. Draft above with violations.
    Options: (1) accept-anyway and continue, (2) provide direction and retry,
    (3) manually edit the draft and tell me to ship the edited version."

  Log the Keeper's choice as a second turn line (--verdict keeper_accepted,
  keeper_edited or keeper_retry; a retry then starts a new turn).
  On (1) accept-anyway: the Keeper's acceptance IS the gate decision for
  this draft. Apply its SCENE_STATE_DELTA (same merge semantics as the
  ship path — skipping it reintroduces the stale-frontmatter bug through
  the surrender door) and append the line "KEEPER: accepted" inside the
  VALIDATOR SURRENDER block. /rp:end treats surrender blocks WITHOUT that
  marker as non-canon: their events never enter ratification.
  On (3): apply the delta only after the Keeper's edit ships, and revise
  the delta first if the edit changed the state facts.
  BREAK.

END LOOP
```

`[validate]` anywhere in the Keeper's input makes the beat consequential, so it is always reviewed, even under `turnGate.review: lint-only`.

### Step 4: Log the turn

Every gate step appends one line to `Work/rp/_validator_log.jsonl`: `check` writes `lint` (and `skipped` when no review runs); the command writes `review`, `adjudicate` and, once per turn, `turn`:

```bash
ASHA_ROOT="${ASHA_ROOT:-$(jq -r '.asha_root // empty' "${ASHA_HOME:-$HOME/.asha}/config.json" 2>/dev/null)}"; python3 "$ASHA_ROOT/plugins/rp/tools/rp_gate.py" log --project-dir . --session-file <SESSION_FILE> --path turn --attempts <ATTEMPT_NUMBER> --verdict <clean|soft_fixed|surrendered|unreviewed> --violations <categories left, if any>
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
| No session file (`source: new`) | Halt, instruct Keeper to run `/rp:start` |
| roleplay-gm spawn fails | Surface error to Keeper; do not retry blindly |
| `rp_gate.py check` fails | Surface the error; offer to review the draft with the routine pass instead (Keeper's choice); log the turn with `--note` |
| continuity-reviewer spawn fails | Surface error and offer to ship the draft unvalidated (Keeper's choice); log the turn with verdict `unreviewed` |
| No rewrites left | Surrender flow as above |

## Arguments

`{command_args}` is the Keeper's free-text input for this turn — what their PC says, does, or instructs. Pass it verbatim as KEEPER_INPUT to roleplay-gm.

ARGUMENTS: {command_args}

## Notes

- This command does NOT replace `/rp:start`. `/rp:start` initializes a session; `/rp:turn` runs each turn within an active session.
- The Keeper can mix `/rp:turn` and ungated input: anything sent without `/rp:turn` goes straight to the GM unvalidated and leaves no gate line.
- A beat is **consequential** when the check reports a trigger: `priced_stake` (SOURCE_LOG, or the priced-stakes phrases in the Keeper's input), `canon_proposed`, `pc_change` (SOURCE_LOG `pc_changed`, or a new PC role in the delta), `arrival_departure` (the Present list changed, or someone arrives on the page), `fight_or_capture`, `validate_tag`, or a project `consequentialPatterns` match. Anything else is **routine**.
- Model calls per beat, excluding the character agents the GM spawns:

  | Beat | Before (0.3.x: review every beat) | Now (defaults) |
  |---|---|---|
  | Routine, lint clean | 2 sonnet | 1 sonnet (the GM) |
  | Routine flagged by the lint, or consequential; review clean or soft only | 2 sonnet (soft findings forced a rewrite: 4) | 1 sonnet + 1 haiku |
  | One confirmed hard violation, fixed by one rewrite | 4 sonnet | 4 sonnet + 1 haiku (GM, routine pass, adjudication, rewrite, re-review) |
  | Worst case, routine (1 rewrite) | 8 sonnet | 4 sonnet + 1 haiku |
  | Worst case, consequential (3 rewrites) | 8 sonnet | 8 sonnet + 1 haiku |

- The table assumes the harness honours a per-spawn `model`, as Claude Code's Task tool does. Codex, Copilot and OpenCode were not checked here: where a spawn ignores it, the routine pass and the adjudicator run on the same model and adjudication buys a second opinion rather than a cheaper first one.
- `turnGate.review: every` restores review on every beat; `lint-only` spends a model call on review only for `[validate]`.
