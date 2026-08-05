---
name: rp-turn
description: "Process a single RP turn through the continuity gate. GM drafts, the reviewer checks against the invariants projection and source provenance, auto-rewrites up to 3 times, surrenders if still failing."
argument-hint: "<keeper turn input as natural language; everything after /rp:turn is the input>"
allowed-tools: ["Task", "Read", "Write", "Edit", "Bash"]
---

# /rp:turn — Validated RP Turn

Wraps a single RP turn with the continuity gate. The Keeper's input goes in; a clean (or surrendered) GM response comes out.

This is the per-turn invocation pattern for guarded RP. Use during active sessions instead of sending free input directly to the GM.

## Project layout assumed

This command uses the conventional RP layout. A project adopting it supplies:

| Path | Role |
|---|---|
| `Work/markers/rp-active` | session marker |
| `Memory/invariants.md` | the compiled invariants projection the gate validates against |
| `Work/rp/rp_session_YYYY-MM-DD.md` | today's session record |
| `Work/rp/_validator_log.jsonl` | per-turn telemetry |

## Preconditions

1. RP session must be active: `Work/markers/rp-active` must exist
2. Invariants document must exist: `Memory/invariants.md` (run `/rp:extract-invariants` first if not)
3. Session file must exist for today

## Protocol

### Step 1: Verify preconditions

```bash
test -f Work/markers/rp-active || echo "ERROR: No active RP session. Run /rp:start first."
test -f Memory/invariants.md || echo "ERROR: No invariants. Run /rp:extract-invariants first."
SESSION_FILE="Work/rp/rp_session_$(date +%Y-%m-%d).md"
test -f "$SESSION_FILE" || echo "WARNING: No session file for today; one will be created."
```

If any precondition fails, halt and inform the Keeper. Do not proceed.

### Step 2: Read current scene state

Read SESSION_FILE for:

- Current scene state YAML (location, time, present, observable)
- Recent register-stack installations (to pass to the gate)
- Day Plan (if exists; needed by the `missing_day_plan` check)

Construct SCENE_STATE block from session file.

### Step 3: Attempt loop (max 4 generations)

```
ATTEMPT_NUMBER = 1
PRIOR_VIOLATIONS = null
MAX_ATTEMPTS = 4   # 1 initial draft + 3 rewrites

LOOP while ATTEMPT_NUMBER <= MAX_ATTEMPTS:

  # 3a. Spawn roleplay-gm to draft narration
  Spawn roleplay-gm via Task:
    subagent_type: "roleplay-gm"
    model: sonnet
    prompt: |
      KEEPER_INPUT: <the {command_args} from /rp:turn>
      SESSION_FILE: <SESSION_FILE>
      SCENE_STATE: <constructed from session file>
      ATTEMPT_NUMBER: <ATTEMPT_NUMBER>
      PRIOR_VIOLATIONS: <PRIOR_VIOLATIONS if attempt > 1>

      Draft the GM response for this turn. If PRIOR_VIOLATIONS is present,
      the previous draft failed validation. Use the suggested_fix entries
      to address each violation in your rewrite.

      Respect the protocol (Day Plan, character agents for profiled NPCs,
      GM_DIRECTIVE on spawns, register-stack persistence, no softening,
      no time skips, no fade-to-black).

      Emit GM_SPAWN_LOG, SOURCE_LOG, and SCENE_STATE_DELTA (changed
      frontmatter keys only — time, location, participants, power) with
      the draft. You cannot write files; the delta is how state changes
      reach the session file.

  Receive DRAFT, GM_SPAWN_LOG, SOURCE_LOG and SCENE_STATE_DELTA from roleplay-gm output.

  # 3b. Spawn the continuity reviewer (live_roleplay mode) to check the draft
  Spawn continuity-reviewer via Task:
    subagent_type: "continuity-reviewer"
    model: sonnet
    prompt: |
      MODE: live_roleplay
      DRAFT: <DRAFT>
      INVARIANTS_FILE: "Memory/invariants.md"
      SESSION_FILE: <SESSION_FILE>
      SCENE_STATE: <constructed scene state>
      ATTEMPT_NUMBER: <ATTEMPT_NUMBER>
      PRIOR_VIOLATIONS: <PRIOR_VIOLATIONS if attempt > 1>
      GM_SPAWN_LOG: <GM_SPAWN_LOG from roleplay-gm>
      SOURCE_LOG: <SOURCE_LOG from roleplay-gm>
      SCENE_STATE_DELTA: <SCENE_STATE_DELTA from roleplay-gm>
      # The delta rides THROUGH the gate, not around it: the reviewer checks
      # every delta entry against the prose (scene_state_mismatch) because a
      # clean verdict commits this delta to the frontmatter.
      PRICED_STAKES: <the `priced_stakes` path from Memory/canon-layout.md;
        default Lore/TTRPG/canon-sources.md when no register exists>
      # Resolve from the register, not a literal: a project keeping its
      # stake register elsewhere would otherwise be validated against a
      # nonexistent file — silently, because a missing source and an
      # unpriced stake look identical to the reviewer.

  Receive VERDICT from continuity-reviewer (clean | violations_found | hard_fail,
  with violations/clean_passes/prior_violation_check fields).

  # 3c. Decide based on verdict
  if VERDICT.verdict == "clean":
    APPEND DRAFT to SESSION_FILE
    APPLY SCENE_STATE_DELTA to SESSION_FILE's YAML frontmatter
      (state rides the same gate as prose: a delta is applied only on a
      clean verdict, so rejected drafts leave no phantom state behind.
      Without this step the frontmatter goes stale and every later turn's
      SCENE_STATE is constructed from the wrong scene.)
      Merge semantics: delta keys are schema dot-paths with scene.* as the
      canonical namespace (scene.time, scene.location, scene.participants,
      scene.mood, scene.power_holder, ...). After applying scene.*, sync the
      root mirrors from it: currentTime := scene.time, currentLocation :=
      scene.location, participants := scene.participants names. The GM never
      emits the root keys directly — one namespace, one merge direction, or
      the mirrors drift apart.
    LOG attempt count and clean-pass categories for telemetry
    OUTPUT DRAFT to Keeper
    BREAK loop

  elif VERDICT.verdict == "violations_found" and ATTEMPT_NUMBER < MAX_ATTEMPTS:
    PRIOR_VIOLATIONS = VERDICT.violations
    ATTEMPT_NUMBER += 1
    LOG attempt as failed with category breakdown
    CONTINUE loop

  elif VERDICT.verdict == "hard_fail" or ATTEMPT_NUMBER >= MAX_ATTEMPTS:
    APPEND DRAFT + violations report to SESSION_FILE under a "## VALIDATOR SURRENDER" block
    LOG surrender event
    OUTPUT DRAFT to Keeper plus violations report plus prompt:
      "Validator surrendered after <ATTEMPT_NUMBER> attempts. Draft above with violations.
      Options: (1) accept-anyway and continue, (2) provide direction and retry,
      (3) manually edit the draft and tell me to ship the edited version."

    On (1) accept-anyway: the Keeper's acceptance IS the gate decision for
    this draft. Apply its SCENE_STATE_DELTA (same merge semantics as the
    clean path — skipping it reintroduces the stale-frontmatter bug through
    the surrender door) and append the line "KEEPER: accepted" inside the
    VALIDATOR SURRENDER block. /rp:end treats surrender blocks WITHOUT that
    marker as non-canon: their events never enter ratification.
    On (3): apply the delta only after the Keeper's edit ships, and revise
    the delta first if the edit changed the state facts.
    BREAK loop

END LOOP
```

### Step 4: Update telemetry log

After every turn (clean or surrendered), append a one-line entry to `Work/rp/_validator_log.jsonl`:

```bash
echo "{\"date\":\"$(date -Iseconds)\",\"session\":\"$SESSION_FILE\",\"attempts\":$ATTEMPTS_USED,\"verdict\":\"$FINAL_VERDICT\",\"clean_categories\":$CLEAN_LIST,\"violation_categories\":$VIOLATION_LIST}" >> Work/rp/_validator_log.jsonl
```

(Create the log file with `mkdir -p Work/rp/ && touch Work/rp/_validator_log.jsonl` if needed.)

### Step 5: Marker persistence

The `Work/markers/rp-active` marker MUST persist through all subagent spawns and the entire turn loop. Never remove it during `/rp:turn` execution.

## Output to Keeper

On clean pass (most turns): just the GM-drafted prose, as if straight from the orchestrator.

On surrender: the draft + the violations report + the three options above.

Do not narrate the validation process to the Keeper unless they ask. The gate runs invisibly when it passes; it only surfaces when it surrenders.

## Failure Modes

| Failure | Handling |
|---|---|
| `Memory/invariants.md` missing | Halt, instruct Keeper to run `/rp:extract-invariants` |
| `Work/markers/rp-active` missing | Halt, instruct Keeper to run `/rp:start` first |
| roleplay-gm spawn fails | Surface error to Keeper; do not retry blindly |
| continuity-reviewer spawn fails | Surface error and offer to ship the draft unvalidated (Keeper's choice) |
| All 4 attempts fail | Surrender flow as above |

## Arguments

`{command_args}` is the Keeper's free-text input for this turn — what their PC says, does, or instructs. Pass it verbatim as KEEPER_INPUT to roleplay-gm.

ARGUMENTS: {command_args}

## Notes

- This command does NOT replace `/rp:start`. `/rp:start` initializes a session; `/rp:turn` runs each turn within an active session.
- The Keeper can mix `/rp:turn` and ungated input: anything sent without `/rp:turn` goes straight to the GM unvalidated. Recommend `/rp:turn` for any turn where the gate's categories carry real weight — tone, stakes, NPC initiative, or a claim about how the world works.
- Latency: average turn is 2 generations (1 GM + 1 reviewer). Worst-case is 7 (4 GM + 3 reviewer). Most turns should clean-pass on attempt 1.
