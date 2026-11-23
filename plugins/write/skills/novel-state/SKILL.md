---
name: write-novel-state
description: "Set up novel manuscript state: bible/state/timeline structure, chapter snapshots, style references, timeline tracking, and source-bound artifact receipts. Use when starting a novel project, organizing manuscript state, or checking whether a review or state snapshot still matches its declared inputs."
license: MIT
---

# Novel State Management Skill

Standardized directory structure for novel state tracking, inspired by proven manuscript management patterns. Enables consistent chapter state snapshots, immutable style references, and timeline tracking.

## When to Use

- Starting a new novel project
- Organizing existing manuscript state
- Setting up state snapshots for chapters
- Establishing immutable style references

## Directory Structure

```
Work/novel/
├── bible/                     # Immutable style reference (append-only)
│   ├── voice.md               # Authoritative voice/style guide
│   ├── rules.md               # Story rules that cannot be broken
│   ├── characters/            # Character sheets (canonical)
│   │   ├── protagonist.md
│   │   └── antagonist.md
│   └── world/                 # Worldbuilding (canonical)
│       ├── magic-system.md
│       └── locations.md
├── state/                     # Per-chapter state snapshots
│   ├── ch01/
│   │   ├── draft-1.md         # First draft
│   │   ├── draft-2.md         # After structure pass
│   │   ├── draft-3.md         # After voice pass
│   │   ├── final.md           # Approved version
│   │   └── metrics.json       # Perplexity gate results
│   ├── ch02/
│   │   └── ...
│   └── current -> ch03/       # Symlink to active chapter
├── timeline/
│   ├── master.md              # Canonical timeline (human-readable)
│   └── events.json            # Structured event log (machine-readable)
└── story/
    ├── synopsis.md            # Story summary
    ├── outline.md             # Chapter-level outline
    └── manuscript.md          # Concatenated final chapters
```

## File Specifications

### bible/voice.md

Immutable after initial creation. Contains authoritative style reference.

```markdown
# Voice Guide

## Author DNA
- Primary influences: [authors/works]
- Sentence rhythm: [cadence description]
- POV approach: [close third, first, omniscient]

## Prohibited Patterns
- [ ] Metronomic sentence length
- [ ] Hedging accumulation ("seemed to", "appeared to")
- [ ] Generic sensory language ("felt a sense of")
- [ ] Adverb stacking

## Required Patterns
- [ ] Varied sentence length (range 5-30 words)
- [ ] Character-specific voice markers
- [ ] Sensory grounding in setting

## Grep Patterns (for automated checks)
```bash
# Hedging accumulation
grep -E "(seemed to|appeared to|felt like|as if)" *.md

# Adverb stacking
grep -E "\w+ly\s+\w+ly" *.md
```

## Example Passages

[3-5 clean exemplar passages demonstrating target voice]

```

### bible/rules.md

Story constraints that cannot be violated without human approval.

```markdown
# Story Rules

## World Constraints
- Magic requires physical cost (fatigue, injury)
- No resurrection - death is permanent
- Technology level: pre-industrial

## Character Constraints
- Protagonist cannot read minds
- Antagonist's motivation must remain hidden until Act 3

## Narrative Constraints
- No deus ex machina
- Chekhov's gun: every introduced element must pay off
```

### state/[chapter]/metrics.json

Per-chapter drafting and review metadata. Store descriptive measurements and human or agent review outcomes, not unsupported authorship scores.

```json
{
  "chapter": "ch01",
  "title": "The Beginning",
  "word_count_target": 3000,
  "drafts": [
    {
      "version": 1,
      "timestamp": "2026-03-09T10:00:00Z",
      "word_count": 2450,
      "review": {
        "status": "changes-requested",
        "reviewers": ["developmental-editor"],
        "findings": 4
      }
    },
    {
      "version": 2,
      "timestamp": "2026-03-09T14:30:00Z",
      "word_count": 2380,
      "review": {
        "status": "approved",
        "reviewers": ["developmental-editor", "line-editor"],
        "findings": 0
      }
    }
  ],
  "status": "approved",
  "approved_date": "2026-03-09T16:00:00Z"
}
```

### timeline/events.json

Structured timeline for consistency checking.

```json
{
  "events": [
    {
      "id": "evt_001",
      "description": "Story begins - protagonist wakes",
      "chapter": "ch01",
      "scene": 1,
      "absolute_date": "1923-10-15",
      "relative_position": 0,
      "characters_present": ["protagonist"],
      "location": "apartment"
    },
    {
      "id": "evt_002",
      "description": "Protagonist meets mentor",
      "chapter": "ch01",
      "scene": 3,
      "absolute_date": "1923-10-15",
      "relative_position": 0.5,
      "characters_present": ["protagonist", "mentor"],
      "location": "cafe"
    }
  ],
  "timeline_start": "1923-10-15",
  "timeline_end": null
}
```

## Workflow Integration

### Chapter Creation with State

1. Create chapter directory:

   ```bash
   mkdir -p Work/novel/state/ch01
   ```

2. Generate Draft 1 → save to `state/ch01/draft-1.md`

3. Run the project-selected editorial reviews → record outcomes in `metrics.json`

4. If changes are requested: revise → save as `draft-2.md`

5. After approval: proceed to finalization

6. Copy the approved draft to `final.md`

7. Update symlink:

   ```bash
   ln -sfn ch01 Work/novel/state/current
   ```

8. Update timeline: Add new events to `events.json`

### Bible Updates

The bible is **APPEND-ONLY** after initial creation:

- **New characters**: Add file to `bible/characters/`
- **New worldbuilding**: Add file to `bible/world/`
- **Voice refinement**: Add examples, never remove existing

**Exceptions require human approval** with documented rationale.

### Source-bound review and state receipts

Use `scripts/artifact_freshness.py` for new review reports or extracted state.
Resolve `ASHA_ROOT` as in Initialization below, and set `PROJECT_ROOT` to the
explicit manuscript project root. All other paths must be normalized relative
paths within that root. Use a new run name for each snapshot and receipt.

Before analysis, declare every manuscript and every authority actually used:

```bash
python3 "$ASHA_ROOT/plugins/write/skills/novel-state/scripts/artifact_freshness.py" capture \
  --project "$PROJECT_ROOT" --manuscript Drafts/ch01.md \
  --source Work/novel/bible/voice.md --source Work/novel/bible/rules.md \
  --source Work/review-config.md --output Work/reports/ch01-run1.inputs.json
```

Repeat `--manuscript` for prose inputs and `--source` for voice, canon, review
configuration, prior state, or other authority. Expand configured directories
to the actual files used. Do not include nonexistent fallback configuration.
`--manuscript` uses the existing strict inline-review clean projection: editorial
comments and frontmatter are excluded. If manuscript frontmatter itself carries
authority, declare that same file with **both** `--manuscript` and `--source`.
Raw authority hashes include frontmatter. Capture a whole file even for a
line-range review. If analysis discovers another dependency, start a new
snapshot with the complete list and rerun the affected analysis.

After writing the report or state files, bind the captured inputs to the outputs:

```bash
python3 "$ASHA_ROOT/plugins/write/skills/novel-state/scripts/artifact_freshness.py" record \
  --project "$PROJECT_ROOT" --snapshot Work/reports/ch01-run1.inputs.json \
  --artifact Work/reports/ch01-run1.md --output Work/reports/ch01-run1.receipt.json
python3 "$ASHA_ROOT/plugins/write/skills/novel-state/scripts/artifact_freshness.py" check \
  --project "$PROJECT_ROOT" --receipt Work/reports/ch01-run1.receipt.json
```

Repeat `--artifact` for each resulting state file. `record` rechecks the captured
inputs and refuses a receipt if they changed during analysis; it never acquires
new input hashes to make old analysis look current. Capture and record refuse
existing output paths. Preserve snapshots, receipts and historical reports;
write a new run after a revision.

Check a receipt before relying on its report or state. Exit 0 / `unchanged`
means only declared input and output bytes still match. Exit 1 / `changed`
identifies a known difference; exit 2 / `unknown` includes missing legacy
receipts, unreadable inputs and malformed projections. For changed or unknown
applicability, inspect the live sources and refresh the affected analysis.
Do not turn this into a block on author edits or retroactively revoke an
author's decision. Receipt checks neither update state/current pointers nor
change canon. They verify no semantic truth, completeness of declared coverage,
or author consent; claim verification and author acceptance remain separate.

Logical symlinks inside the project, including `state/current`, are supported.
Retargeting to different bytes is detected; retargeting to identical bytes is
`unchanged`, which makes no claim about file identity. These are ordinary local
JSON receipts, not tamper-resistant attestations or a new canonical state store.

### Context Loading

For efficient context management, load only what's needed:

```
# Active work (always load)
state/current/          # Current chapter drafts
bible/voice.md          # Voice reference

# On demand (load when needed)
bible/characters/X.md   # When X appears in scene
bible/world/Y.md        # When Y location/system used
timeline/events.json    # For consistency checks

# Never load unless requested
state/ch*/              # Historical drafts
story/manuscript.md     # Full concatenated text
```

## Initialization

Initialize with the helper, the same one `/write:init-novel` runs. It creates
only what is missing (directories, template files and an empty
`timeline/events.json`) and leaves existing files untouched, so it is safe on a
project that already has a bible, story files or a timeline:

```bash
ASHA_ROOT="${ASHA_ROOT:-$(jq -r '.asha_root // empty' "${ASHA_HOME:-$HOME/.asha}/config.json" 2>/dev/null)}"
[[ -n "$ASHA_ROOT" ]] || { echo "ERROR: asha_root unresolved — run ./install.sh or launch via the asha wrapper" >&2; exit 1; }
python3 "$ASHA_ROOT/plugins/write/skills/novel-state/scripts/init_novel_state.py" /path/to/project
```

The path is the project that holds `Work/novel`; without it the helper uses
the current directory. Do not write these files by hand with shell
redirection, which replaces a file that already exists.

## Context Efficiency

**Token savings through structure**:

| Approach | Tokens | When |
|----------|--------|------|
| Load current chapter only | ~2k | Active drafting |
| Load current + voice | ~3k | Voice pass |
| Load current + relevant character | ~4k | Character scene |
| Load full state | ~15k+ | Avoid unless needed |

**Rule**: Load `current` symlink contents, not full `state/` directory.

## Coordination with Other Skills

| Skill | Relationship |
|-------|--------------|
| **developmental-editor** | Supplies structural review outcomes for `metrics.json` |
| **line-editor** | Supplies sentence-level review outcomes for `metrics.json` |
| **continuity-reviewer** | Reads `bible/` and `timeline/` |
| **book-export** | Reads `story/manuscript.md` |

## Files

| File | Purpose |
|------|---------|
| `SKILL.md` | This documentation |
| `scripts/init_novel_state.py` | Initialize directory structure |
| `scripts/artifact_freshness.py` | Capture inputs, record output receipts, check declared applicability |

## Related

- `write/agents/continuity-reviewer.md` — Validates against bible
- `write/recipes/chapter-creation.yaml` — Chapter workflow using state
