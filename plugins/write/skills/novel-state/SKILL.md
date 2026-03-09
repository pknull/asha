---
name: novel-state
description: Novel writing state management structure. Documents the bible/state/timeline directory pattern for tracking manuscript state across sessions.
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

Per-chapter metrics from perplexity gate and other quality checks.

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
      "perplexity_gate": {
        "verdict": "FAIL",
        "metrics": {
          "mean_perplexity": 24.3,
          "low_ppl_ratio": 0.32,
          "consecutive_low_max": 5
        }
      }
    },
    {
      "version": 2,
      "timestamp": "2026-03-09T14:30:00Z",
      "word_count": 2380,
      "perplexity_gate": {
        "verdict": "PASS",
        "metrics": {
          "mean_perplexity": 31.8,
          "low_ppl_ratio": 0.18,
          "consecutive_low_max": 2
        }
      },
      "rewrite_method": "VS-Tail"
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

3. Run perplexity gate → record results in `metrics.json`

4. If FAIL: Apply VS-Tail rewrite → save as `draft-2.md`

5. If PASS: Proceed to structural edit

6. After approval: Copy to `final.md`

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

### Using the init script

```bash
python scripts/init_novel_state.py /path/to/project
```

### Manual initialization

```bash
cd /path/to/project
mkdir -p Work/novel/{bible/characters,bible/world,state,timeline,story}

# Create template files
cat > Work/novel/bible/voice.md << 'EOF'
# Voice Guide

## Author DNA
(Define influences and style here)

## Prohibited Patterns
(List patterns to avoid)

## Required Patterns
(List patterns to include)
EOF

cat > Work/novel/bible/rules.md << 'EOF'
# Story Rules

## World Constraints
(Immutable world rules)

## Character Constraints
(Character limitations)

## Narrative Constraints
(Story structure rules)
EOF

echo '{"events":[]}' > Work/novel/timeline/events.json

cat > Work/novel/story/synopsis.md << 'EOF'
# Synopsis

(Story summary)
EOF

cat > Work/novel/story/outline.md << 'EOF'
# Outline

## Act 1
- Chapter 1:
- Chapter 2:

## Act 2
...
EOF
```

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
| **perplexity-gate** | Writes to `metrics.json` |
| **consistency-checker** | Reads `bible/` and `timeline/` |
| **book-export** | Reads `story/manuscript.md` |

## Files

| File | Purpose |
|------|---------|
| `SKILL.md` | This documentation |
| `scripts/init_novel_state.py` | Initialize directory structure |

## Related

- `write/skills/perplexity-gate/` — Quality gate populating metrics
- `write/agents/consistency-checker.md` — Validates against bible
- `write/recipes/chapter-creation.yaml` — Chapter workflow using state
