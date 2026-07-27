---
name: rp-extract-invariants
description: "Extract or refresh Memory/invariants.md from canonical sources. Run after major canon shifts, new character files, or arc transitions. Re-runs preserve KEEPER-LOCKED sections."
allowed-tools: ["Task", "Read", "Write"]
---

# /rp:extract-invariants

Spawns the `rp-invariants-extractor` agent to build or refresh `Memory/invariants.md` from all canonical sources.

## When to run

- **First-time setup**: before the first `/rp:turn` (the validator requires the invariants doc to exist)
- **After arc transitions**: when an arc closes and new arc state takes effect
- **After new character canon**: when a new NPC file is added to `Lore/World/Characters/`
- **After major mechanic changes**: when cycle/architecture/cosmology canon shifts
- **After feedback updates**: when new `feedback_rp_*.md` files are added to auto-memory
- **Periodically**: every 5-10 sessions to catch drift

## What it does

1. Reads canonical sources in tier order (CLAUDE.md, auto-memory, protocols, state files, summaries, character canon, cosmology, project bibles, protocol commands)
2. If `Memory/invariants.md` exists, preserves all `KEEPER-LOCKED` sections verbatim
3. Updates auto-extractable sections with current canon
4. Flags conflicts when sources contradict
5. Writes `Memory/invariants.md` and returns a summary

## Protocol

### Step 1: Spawn the extractor

Spawn `rp-invariants-extractor` via Task:

```yaml
subagent_type: "rp-invariants-extractor"
model: sonnet
prompt: |
  Run a full invariants extraction.

  TARGET_FILE: "Memory/invariants.md"
  MODE: refresh  # preserve existing KEEPER-LOCKED sections

  Read all canonical sources per the tier order in your agent definition,
  synthesize into the structured invariants schema, and write the result.

  Return a structured summary including: sources_read count, sections_updated,
  keeper_locked_preserved, conflicts_flagged, new_canon_detected.
```

### Step 2: Surface the summary to the Keeper

Present the agent's return summary as-is. Highlight:

- Number of sections updated
- Any conflicts flagged (these need Keeper attention)
- Any new canon detected (the Keeper may want to review the additions)
- KEEPER-LOCKED sections preserved (count or list)

### Step 3: Recommend a Keeper review

After the agent returns, suggest:

> **Recommendation**: Open `Memory/invariants.md` and skim. Any section that should not be auto-overwritten on next extraction can be marked KEEPER-LOCKED by adding `[KEEPER-LOCKED]` to the section header and adding the section name to the `keeper_locked_sections` list in frontmatter.

Do not auto-edit the doc on the Keeper's behalf — they review.

## Notes

- This command is idempotent. Running it twice in a row produces the same output (modulo timestamp).
- The extractor never writes to `Lore/`. Only `Memory/invariants.md` is created/edited.
- The extractor also reads the project's auto-memory directory — `~/.claude/projects/<project-key>/memory/`, where `<project-key>` is the cwd-derived key for this project — in addition to in-vault sources.
- Run time: typically 30-90 seconds depending on how many sources have changed.
