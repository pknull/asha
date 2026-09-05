---
name: rp-extract-invariants
description: "Extract or refresh Memory/invariants.md from canonical sources. Run after major canon shifts, new character files, or arc transitions. Re-runs preserve KEEPER-LOCKED sections."
allowed-tools: ["Read", "Write", "Grep", "Glob"]
---

# /rp:extract-invariants

Build or refresh `Memory/invariants.md` directly from the project's canonical
sources. This command performs the extraction itself; it does not delegate the
work to an agent.

## When to run

- **First-time setup**: before the first `/rp:turn` (the validator requires the invariants doc to exist)
- **After arc transitions**: when an arc closes and new arc state takes effect
- **After new character canon**: when a new NPC file is added to the registered character paths
- **After major mechanic changes**: when cycle, architecture, or cosmology canon shifts
- **After feedback updates**: when new RP feedback files are added to project memory
- **Periodically**: every 5-10 sessions to catch drift

## What it does

1. Resolves canonical sources through `Memory/canon-layout.md`, falling back to the documented template defaults only when the register is absent
2. Reads those sources in authority order and records the files actually read
3. Preserves every existing `KEEPER-LOCKED` section verbatim
4. Refreshes auto-extractable sections, flagging contradictions instead of silently choosing a winner
5. Writes `Memory/invariants.md` and returns a summary

## Protocol

### Step 1: Discover and read the sources

1. Read `Memory/canon-layout.md` when present. Resolve only the project-relative
   globs declared there. If it is absent, use the defaults documented in the RP
   plugin's `templates/canon-layout.md` and say that the fallback was used.
2. Read project-level instruction and canon-register files first, then authored
   canon, curated session material, raw session records, and finally derived
   memory or feedback. Within a tier, current files on disk outrank summaries
   or recollections.
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

### Step 3: Extract the projection

Extract only claims supported by the sources that were actually read. Include
the sections consumed by the live continuity gate:

- **Tone Anchors** — binding tonal limits and prohibited softening
- **Character Registers** — voice, knowledge, relationship, and physical-state constraints by character
- **Mechanical Rules** — canonical system behavior and priced consequences
- **World and Timeline Facts** — setting, chronology, location, faction, and object constraints
- **Protocol Requirements** — Day Plan, character delegation, scene-state, source-log, and register-stack requirements
- **Conflicts and Gaps** — contradictory sources, empty registered globs, and facts that cannot be resolved without Keeper judgment

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

### Step 5: Return the summary

Return:

```yaml
sources_read: 0
sections_updated: []
keeper_locked_preserved: []
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
- Only `Memory/invariants.md` is created or edited.
- The projection is a read-speed cache over authored canon, not a replacement for it.
