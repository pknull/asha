---
name: rp-extract-invariants
description: "Extract or refresh Memory/invariants.md, static canon only, from canonical sources. Run after major canon shifts, new character files, arc transitions, or a ratifying /rp:end. Re-runs preserve KEEPER-LOCKED static sections."
allowed-tools: ["Read", "Write", "Grep", "Glob", "Bash"]
---

# /rp:extract-invariants

Build or refresh `Memory/invariants.md` directly from the project's canonical
sources. This command performs the extraction itself; it does not delegate the
work to an agent.

The projection holds **static canon only**: what stays true while play goes on.
Current state (where everyone is, who is alive, the day and beat, stats,
workings, clocks) changes every beat, so the gate reads it from the live session
file and the run's state file instead, and warns when the projection carries it
(`rp_gate.py`).

## When to run

- **First-time setup**: before the first `/rp:turn` (the validator requires the invariants doc to exist)
- **After arc transitions**: when an arc closes and its outcome is ratified into canon
- **After a ratifying `/rp:end`**: the gate warns when `last_extraction` predates the run's state
- **After new character canon**: when a new NPC file is added to the registered character paths
- **After major mechanic changes**: when cycle, architecture, or cosmology canon shifts
- **After feedback updates**: when new RP feedback files are added to project memory
- **Periodically**: every 5-10 sessions to catch drift

## What it does

1. Resolves canonical sources through `Memory/canon-layout.md`, falling back to the documented template defaults only when the register is absent
2. Reads those sources in authority order and records the files actually read
3. Preserves every existing `KEEPER-LOCKED` static section verbatim
4. Refreshes auto-extractable sections with static canon only, flagging contradictions instead of silently choosing a winner
5. Writes `Memory/invariants.md`, checks it for live state with `rp_gate.py invariants`, and returns a summary

## Protocol

### Step 1: Discover and read the sources

1. Read `Memory/canon-layout.md` when present. Resolve only the project-relative
   globs declared there. If it is absent, use the defaults documented in the RP
   plugin's `templates/canon-layout.md` and say that the fallback was used.
2. Read project-level instruction and canon-register files first, then authored
   canon, curated session material, raw session records, and finally derived
   memory or feedback. Within a tier, current files on disk outrank summaries
   or recollections. In a session record, skip every `> [!struck]` callout: a
   draft the turn gate struck, kept as a record, never canon.
3. Record every path and glob searched. An empty glob is evidence of a search,
   not evidence that the corresponding canon does not exist.
4. If the project's Claude auto-memory directory is available, read relevant
   RP memory after in-project canonical sources and treat it as derived notes,
   never as authority over disk.

### Step 2: Preserve Keeper locks

If `Memory/invariants.md` already exists, read it before drafting the replacement.
Copy every section whose heading contains `[KEEPER-LOCKED]` byte-for-byte, and
preserve the corresponding names from the frontmatter
`keeper_locked_sections` list. Never summarize, reorder, or repair a locked
section. If the list and headings disagree, preserve the union and report the
metadata mismatch as a conflict.

Before drafting, check the existing file with the gate's own deterministic
test (no model call; it reads and writes nothing):

```bash
ASHA_ROOT="${ASHA_ROOT:-$(jq -r '.asha_root // empty' "${ASHA_HOME:-$HOME/.asha}/config.json" 2>/dev/null)}"; python3 "$ASHA_ROOT/plugins/rp/tools/rp_gate.py" invariants --project-dir .
```

Its `live_state` warning lists each hit with `locked: true` or `locked: false`.
A hit with `locked: true` is live state inside KEEPER-LOCKED content (such as
a locked Timeline State): it is the Keeper's to retire, so neither carry it nor
drop it. Stop here, write nothing, and report each such hit under
`locked_live_state`, asking the Keeper to retire it (remove the section and its
name from `keeper_locked_sections`, and move anything the run still needs into
the run's state file). Treat a locked section you judge to hold live state
(Step 3) the same way, even when the tool did not flag it. Hits with
`locked: false` are unlocked live state: leave them out of the replacement.

### Step 3: Extract the projection

Extract only claims supported by the sources that were actually read, and only
static canon. Include the sections consumed by the live continuity gate:

- **Tone Anchors** — binding tonal limits and prohibited softening
- **Character Registers** — voice, knowledge, relationship, and physical constraints by character, as the canonical sheet sets them
- **Mechanical Rules** — canonical system behavior and priced consequences
- **World and Timeline Facts** — setting, settled chronology (closed arcs, the calendar), location, faction, and object constraints
- **Protocol Requirements** — Day Plan, character delegation, scene-state, source-log, and register-stack requirements
- **Conflicts and Gaps** — contradictory sources, empty registered globs, and facts that cannot be resolved without Keeper judgment

Never write live state. It changes during play; the gate reads it from the
session file's frontmatter and header and from the run's state file the session
names (`rp_gate.py session` reports where). Live state is any of:

- a Timeline State, Run state, Live clocks, Current vessel or Resume point section, heading or label;
- where anyone is now, who is present, alive or dead in the run, and the current day, time or beat;
- current stats, conditions, workings, possessions, debts and running clocks;
- the status of pending canon;
- a rule's worked example that embeds current values: state the rule, and let the gate read the values.

A run's events become static canon only when ratified into the project's canon
(`/rp:end`); extract them from that canon then, never from the run's state.

For each extracted claim, retain a concise source-path citation. Do not invent a
fact to fill an empty section, and do not treat the projection itself as a
source of canon.

### Step 4: Assemble and write once

Assemble the complete replacement in memory before writing. Its YAML
frontmatter must include:

```yaml
last_extraction: YYYY-MM-DD
keeper_locked_sections: []
sources_read: []
conflicts_flagged: 0
```

Restore locked sections verbatim in their existing positions where possible;
write refreshed sections around them. Write only `Memory/invariants.md`. Never
write to a path registered as authored canon.

### Step 5: Verify that no live state was written

Run the gate's own check on the written file (deterministic, no model call; it
reads and writes nothing):

```bash
ASHA_ROOT="${ASHA_ROOT:-$(jq -r '.asha_root // empty' "${ASHA_HOME:-$HOME/.asha}/config.json" 2>/dev/null)}"; python3 "$ASHA_ROOT/plugins/rp/tools/rp_gate.py" invariants --project-dir . --strict
```

Exit 1 means the projection still carries live state: the `live_state`
warning lists its hits. Remove each hit with `locked: false` and write once
more. Never edit locked text: a hit with `locked: true` cannot appear after a
clean Step 2 unless a locked section was changed, so write the previous file
back unchanged (you read it in Step 2) and report the hit under
`locked_live_state`. A `stale` or `no_extraction_date` warning after a fresh
extraction means the `last_extraction` frontmatter date is wrong. Exit 2 means
the file was not written.

### Step 6: Return the summary

Return:

```yaml
sources_read: 0
sections_updated: []
keeper_locked_preserved: []
locked_live_state: []   # locked sections holding live state; nothing was written
live_state_excluded: []  # live-state claims found in the sources and left out
conflicts_flagged: []
new_canon_detected: []
empty_globs: []
```

Highlight conflicts and new canon for Keeper review. Then recommend:

> **Recommendation**: Open `Memory/invariants.md` and skim. Any section that
> should not be auto-overwritten on the next extraction can be marked
> KEEPER-LOCKED by adding `[KEEPER-LOCKED]` to its heading and its section name
> to the `keeper_locked_sections` frontmatter list.

Do not make follow-up edits after presenting the summary unless the Keeper asks.

## Notes

- This command is idempotent. Running it twice in a row produces the same semantic output, apart from extraction metadata.
- Only `Memory/invariants.md` is created or edited; the Step 5 check only reads.
- The projection is a read-speed cache over authored canon, not a replacement for it.
