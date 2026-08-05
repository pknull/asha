---
name: rp-end
description: "Close an active roleplay session — clears the marker, writes summary and character state, runs per-item canon ratification, and promotes only ratified items to project canon."
argument-hint: "[--quick | --canon-only | --no-commit]"
allowed-tools: ["Task", "Read", "Write", "Edit", "Bash", "Grep", "Glob"]
---

# /rp:end — Close a Roleplay Session

Ends an active roleplay session with canon synthesis and state management.

## What This Does

1. **Removes rp-active marker** — Re-enables session logging
2. **Saves session file** — Ensures all content written to disk
3. **Generates session summary** — Creates `Work/rp/summaries/` entry
4. **Updates character states** — Writes `Work/rp/characters/*.md` with current stats
5. **Ratifies canon (NEW)** — Per-item Keeper accept/reject/defer via `rp-ratifier` agent against `Memory/invariants.md`
6. **Synthesizes ratified canon** — Promotes only accepted items to Vault via `canon-writer`
7. **Optional: Git commit** — Commits session artifacts

## Session End Protocol

### Step 1: Remove RP Marker

```bash
rm -f Work/markers/rp-active
```

This re-enables session watching for subsequent work.

### Step 2: Save Session File

Ensure the current session file is written:

- Location: `Work/rp/rp_session_YYYY-MM-DD.md`
- Use Edit/Write tool (never claim saved without tool use)

### Step 3: Generate Session Summary

Create or update `Work/rp/summaries/YYYY-MM-DD.md`:

```markdown
---
session_date: "2026-02-03"
fiction_date: "<fiction-date>"
participants: ["Alder", "Sable", "the Proprietor"]
location: "Ashfield House"
---

# Session Summary: 2026-02-03

## Events

- Alder visited Ashfield House seeking information
- Met the Proprietor, established initial terms
- Discovered back room and the Rival's Academy connection

## Character States

- **Alder**: HP 12/12, MP 38/90, SAN 65/75
- **Sable**: Hunger level adequate, contained in Threshold

## Canon Established

- Ashfield House (new location)
- the Rival's Academy connection
- The Siphon artifact introduced

## Threads Open

- the Proprietor's true purpose unclear
- the Rival requires follow-up
- The Siphon's location unknown
```

### Step 4: Update Character States

Write/update `Work/rp/characters/{character}_state.md`:

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
  SESSION_FILE: "Work/rp/rp_session_YYYY-MM-DD.md"
  INVARIANTS_FILE: "Memory/invariants.md"
  SUMMARY_FILE: "Work/rp/summaries/YYYY-MM-DD.md"

  Identify all canon-worthy additions from the session. EXCLUDE content
  inside "## VALIDATOR SURRENDER" blocks unless the block carries the
  "KEEPER: accepted" marker — a surrendered draft failed the continuity
  gate, and only the Keeper's explicit acceptance (recorded by /rp:turn)
  makes its events real. Canonizing an unmarked surrender block promotes
  the exact fabrications the gate caught. For each addition:
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
  SESSION_FILE: "Work/rp/rp_session_2026-02-03.md"
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
