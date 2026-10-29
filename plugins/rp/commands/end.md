---
name: rp-end
description: "Close an active roleplay session — clears the marker, writes summary and character state, runs per-item canon ratification, and promotes only ratified items to project canon."
argument-hint: "[--quick | --canon-only | --no-commit]"
allowed-tools: ["Task", "Read", "Write", "Edit", "Bash", "Grep", "Glob"]
---

# /rp:end — Close a Roleplay Session

Ends an active roleplay session with canon synthesis and state management.

## What This Does

1. **Removes rp-active marker** — Ends RP routing and safeguards
2. **Saves session file** — Ensures all content written to disk
3. **Stitches the session summary** — Each closed fiction day already has its summary (`/rp:turn` writes it when rotation closes the day); this writes only the open day's part and stitches the rest into a `Work/rp/summaries/` entry
4. **Updates character states** — With a `run:` block, refreshes the run state file's generated block (no model call); without one, writes `Work/rp/characters/*.md` as before
5. **Ratifies canon (NEW)** — Per-item Keeper accept/reject/defer via `rp-ratifier` agent against `Memory/invariants.md`
6. **Synthesizes ratified canon** — Promotes only accepted items to Vault via `canon-writer`
7. **Optional: Git commit** — Commits session artifacts

## Session End Protocol

### Step 1: Resolve the session file, then remove the RP marker

First collect any pending review, exactly as `/rp:turn` Step 1b does (with no new input to hold): a beat shown before its review is provisional, and the END block, the stitched summary and ratification read only settled beats. `rp_gate.py pending --project-dir .` lists them; when the background verdict is not in hand, run the review now. A struck beat is redrafted through the `/rp:turn` loop before the session closes, unless the Keeper says to close on the struck record. `rotate --repair`, `summary --stitch` and `state --refresh` refuse while a provisional beat is pending. Then `rp_gate.py pending --project-dir . --clear-queued --clear-carried`: it prints, and removes, an input still held after a strike and the run's carried soft findings; name each in the END block (held input: not played; carried findings: never applied), so neither leaks into the next session.

The marker names the session file, so resolve it first. `rotate --repair` moves no beat and opens no file: it closes a file an interrupted rotation left without its footer, hides (renames to `.day-NNNN.md.orphan-<date>`, never deletes) a day file such a crash left unused, and writes any struck drafts still waiting into the current file, before the END block:

```bash
ASHA_ROOT="${ASHA_ROOT:-$(jq -r '.asha_root // empty' "${ASHA_HOME:-$HOME/.asha}/config.json" 2>/dev/null)}"
[[ -n "$ASHA_ROOT" ]] || { echo "ERROR: asha_root unresolved — run ./install.sh or launch via the asha wrapper" >&2; exit 1; }
python3 "$ASHA_ROOT/plugins/rp/tools/rp_gate.py" rotate --repair --project-dir . >/dev/null
python3 "$ASHA_ROOT/plugins/rp/tools/rp_gate.py" session --project-dir .
```

Use the printed `path` as SESSION_FILE for every later step: the current day file, where the END block goes. `session_files` lists every file this session wrote, oldest first (one per fiction day it crossed; a marker from before day files, which records no `session_opened`, reaches back to the legacy log the session opened), and `session_opened_line` is where it began in the first (null: from the first file's start). If it prints a warning (the marker named no session file), tell the Keeper which file it chose before going on. Then:

```bash
rm -f Work/markers/rp-active
```

This ends RP-specific routing for subsequent work.

### Step 2: Save Session File

Ensure the current session file is written:

- Location: SESSION_FILE (from Step 1). Append the END block (where play stopped, state, open clocks) at its end. Play stopped at the last beat: Step 1 may have just written struck drafts after it, and a `> [!struck]` block is never where play stopped
- A legacy `Work/rp/rp_session_*.md` log: set `status: closed` in its YAML frontmatter, so the session-file fallback no longer picks it. A day file stays open: only rotation closes it, and the next `/rp:start` continues it or rotates from it
- Never edit an earlier file in `session_files`: rotation closed it, and its `file:line` citations must stay valid (cite a line only in a closed file: an open file's lines move with every frontmatter delta)
- Use Edit/Write tool (never claim saved without tool use)

### Step 3: Stitch the Session Summary

Each fiction day this session closed already has its summary: `/rp:turn` writes it, with one model call, when rotation closes the day. Pass the first file in Step 1's `session_files` as `--opened`, since the marker is gone:

```bash
ASHA_ROOT="${ASHA_ROOT:-$(jq -r '.asha_root // empty' "${ASHA_HOME:-$HOME/.asha}/config.json" 2>/dev/null)}"; python3 "$ASHA_ROOT/plugins/rp/tools/rp_gate.py" summary --project-dir . --session-file "<SESSION_FILE>" --opened "<session_files[0]>"
```

`days` lists each day the session closed, and any earlier closed day still without a summary (one `/rp:start` closed, say), with its summary `path` and whether it `exists`. Write each that is missing as `/rp:turn` Step 3e does: timeline-search on haiku with the entry's `beats` and `plans` (each as its `parts` when it has them: the lines between are struck drafts), then `summary --project-dir . --session-file "<SESSION_FILE>" --day N --publish Work/rp/.gate/day-summary.md`; one model call per missing day. If a summariser from the last rotation may still be running, wait for it first. `open` is the current day so far, with its `beats` and `plans` ranges; its file is still open.

Then write `Work/rp/.gate/session-extra.md`, the one part of the summary written here:

- the session's own sections, before any day (Keeper rulings and steers, OOC notes);
- a `## Day N` section for the open day so far (from `session_opened_line` when the session opened in that file), and for any earlier day of the session in a legacy log that no day summary covers;
- closing sections after the days (threads open, canon proposed for ratification).

Cite each beat by number (`B4123`), and a line only in a file rotation has footered (`<day file>:<line>`; the open day's file is still open, so cite its beats, never its lines). Mark inference INFERRED and never invent; the PC's thoughts and choices are the Keeper's, and are never written beyond what the Keeper wrote. Leave out every `> [!struck]` block: a draft the gate struck is never an event, a state or canon; name one, if at all, only under a "Struck drafts (not canon)" line, by beat and attempt. Then stitch:

```bash
ASHA_ROOT="${ASHA_ROOT:-$(jq -r '.asha_root // empty' "${ASHA_HOME:-$HOME/.asha}/config.json" 2>/dev/null)}"; python3 "$ASHA_ROOT/plugins/rp/tools/rp_gate.py" summary --project-dir . --session-file "<SESSION_FILE>" --opened "<session_files[0]>" --stitch --extra Work/rp/.gate/session-extra.md --out Work/rp/summaries/YYYY-MM-DD.md
```

It writes, under generated frontmatter, the extra text's opening sections, every day (the day summaries and the extra text's days, in day order; a section between two days stays with the day before it), its closing sections, and last its "State at close": the extra text's own (place, gear, what the `run:` block does not hold), with a copy of the `run:` block under it. It refuses while a closed day of the session has no summary or the extra text repeats one, and it never overwrites a summary: the same stitch again changes nothing, a different one is refused. A campaign without day files (no `log_dir`) has no day summaries, so the extra text covers the whole session, as before. The output name may follow the project's own convention (`YYYY-MM-DD_<campaign>-day-<first>-<last>.md`), under `Work/rp/summaries/`.

### Step 4: Update Character States

With a `run:` block in the session frontmatter, refresh the run state file's generated block (no model call). It keeps every hand-written line, and refuses (tell the Keeper) a block edited by hand:

```bash
ASHA_ROOT="${ASHA_ROOT:-$(jq -r '.asha_root // empty' "${ASHA_HOME:-$HOME/.asha}/config.json" 2>/dev/null)}"; python3 "$ASHA_ROOT/plugins/rp/tools/rp_gate.py" state --refresh --project-dir . --session-file "<SESSION_FILE>"
```

The stitched summary's state at close and the block both come from the `run:` block, never from the prose. Write `Work/rp/characters/{character}_state.md` only for a campaign without one, from the beats and the END block, never from a `> [!struck]` block:

```markdown
---
character: "Alder"
last_updated: "2026-02-03"
fiction_date: "<fiction-date>"
---

# Alder - Current State

## Stats
- HP: 12/12
- MP: 38/90
- SAN: 65/75

## Conditions
- None

## Inventory Changes
- [list any changes]

## Relationship Updates
- the Proprietor: Initial contact established
```

### Step 5: Canon Ratification (NEW — gate before promotion)

Before any canon is promoted to `Lore/`, the Keeper ratifies each proposed addition. This protects against auto-promoting items the Keeper does not want canonized, and against promoting items that conflict with existing invariants.

**Preconditions**:

- `Memory/invariants.md` should exist. If absent, log a warning and skip ratification (canon-writer in Step 6 will run unguarded as fallback). Recommend running `/rp:extract-invariants` after the session.

**Spawn the rp-ratifier agent**:

```yaml
subagent: rp-ratifier
prompt: |
  SESSION_FILE: <SESSION_FILE from Step 1>
  SESSION_FILES: <session_files from Step 1>  # the whole session, oldest first; read from session_opened_line in the first
  INVARIANTS_FILE: "Memory/invariants.md"
  SUMMARY_FILE: "Work/rp/summaries/YYYY-MM-DD.md"

  Identify all canon-worthy additions from the session. EXCLUDE every
  "> [!struck]" callout block: a draft the continuity gate struck, kept as a
  record. A surrendered draft the Keeper accepted was shipped as a beat of
  its own, so the struck copy is never the source. Also exclude a legacy
  "## VALIDATOR SURRENDER" block (from before struck drafts) unless it
  carries the "KEEPER: accepted" marker. Canonizing struck or unmarked
  surrendered text promotes the exact fabrications the gate caught. For
  each addition:
  1. Categorize against invariants (extends_existing | new_canon | conflicts_with_invariants | pending_resolution)
  2. Surface to Keeper via AskUserQuestion for accept / reject / defer (and accept-and-update-invariants for conflicts)
  3. Route accepted items into the canon-writer queue
  4. Write deferred items to Work/rp/pending-canon/
  5. Drop rejected items
  6. Apply any invariants updates the Keeper authorized

  Return a structured summary of the ratification.
```

The ratifier returns `accepted_for_canon_writer` — pass that list to canon-writer in Step 6 (only the accepted items get promoted).

### Step 6: Canon Synthesis (executes only on accepted items)

**This is the key step.** Like `/session:save` updates Memory, this updates the Vault. **Runs only on items the Keeper accepted in Step 5.**

Spawn the **canon-writer** agent:

```yaml
subagent: canon-writer
prompt: |
  SESSION_FILE: <SESSION_FILE from Step 1>
  SESSION_FILES: <session_files from Step 1>
  FACT_TYPE: "all"
  RATIFIED_ITEMS: <accepted_for_canon_writer list from rp-ratifier>

  Promote ONLY the ratified items. For each:
  - Determine if Vault file exists
  - Create new or edit existing
  - Match existing patterns/style
  - Tag with "established: Session YYYY-MM-DD"

  Skip any item not in RATIFIED_ITEMS.
```

**Canon Promotion Targets:**

Resolve every destination from `Memory/canon-layout.md` (*Entity paths* table) — the same register `world-lookup` reads for lookups, so a project that relocates its canon edits one file rather than every agent. Fall back to the defaults below only when the register is absent, and say so before writing.

| Type | Destination (default — see register) | Update Style |
|------|-------------|--------------|
| Locations | `location` → `Lore/World/Places/` | Create or expand |
| Characters | `character` → `Lore/World/Characters/` | Add relationships, details |
| Artifacts | `artifact` → `Lore/World/Magic/Artefacts/` | Create new |
| Magic | `magic` → `Lore/World/Magic/` | Document new spells/effects |
| Factions | `faction` → `Lore/World/Factions/` | Update membership, goals |

Never invent a destination. If a ratified item has no matching kind in the register, stop and ask the Keeper where it belongs — writing canon to a guessed path scatters the source of truth, and the next lookup will not find it.

### Step 7: Git Commit (Optional)

If user confirms, commit session artifacts:

```bash
git add Work/rp/
# Only if canon was promoted: stage the exact files canon-writer reported —
# each of its operations names its file_path/path, so the promotion report IS
# the stage list. Never stage a guessed "canon root": the register maps kinds
# to globs under potentially unrelated roots, so no single directory exists to
# add, and a guessed one silently omits promoted files outside it.
git add <each file path from canon-writer's promotion report>
git commit -m "Session save: [brief summary from Step 3]"
```

## Canon Promotion Guidelines

**PROMOTE when:**

- Location was described in detail (more than passing mention)
- Character revealed new background/motivation
- Artifact/item has established properties
- Relationship significantly changed
- World fact was established (not just speculated)

**DON'T PROMOTE when:**

- Fact is uncertain/speculative in session
- Detail was throwaway/atmospheric
- Would contradict existing canon
- Player hasn't confirmed (for PC details)

**DEFER (to Work/rp/pending-canon/) when:**

- Uncertain if permanent
- Needs Keeper review
- May conflict with planned story

## Integration with Session Save

This command is **parallel** to `/session:save`, not a replacement:

- `/session:save` → Memory Bank (project context, learnings)
- `/rp:end` → Lore (world canon, character states)

Run `/session:save` separately if session also produced technical/project learnings.

## Arguments

Parse {command_args}:

- Empty → Full protocol (all steps)
- `--quick` → Steps 1-3 only (no canon synthesis)
- `--canon-only` → Steps 5-6 only (assumes session already saved)
- `--no-commit` → Skip git commit

ARGUMENTS: {command_args}
