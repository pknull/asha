# Write Plugin

**Version**: 1.3.0

Creative writing workflows for fiction development: prose craft, worldbuilding, editing, AI detection, and storytelling agents.

## Installation

```bash
/plugin install write@asha-marketplace
```

## Features

### Agents

| Agent | Role |
|-------|------|
| **outline-architect** | Story structure, beat sheets, chapter outlines |
| **prose-writer** | Draft generation with voice anchoring |
| **fiction-writer** | Primary creative coordinator for full pipeline |
| **consistency-checker** | Continuity tracking (characters, timelines, lore) |
| **developmental-editor** | Structural analysis (arcs, pacing, theme) |
| **line-editor** | Sentence craft, rhythm, polish |
| **prose-analysis** | Multi-mode prose review (voice, continuity, coherence) |
| **intimacy-designer** | Adult content specialist (scene frameworks, boundary arbitration) |
| **manuscript-editor** | Structural editing and revision coordination |

### Commands

| Command | Purpose |
|---------|---------|
| `/write:perplexity` | Check prose for AI flatness using local perplexity |
| `/write:init-novel` | Initialize novel state directory structure |
| `/write:review-section` | Run periodic review suite on completed section |

### Skills

| Skill | Purpose |
|-------|---------|
| **perplexity-gate** | Local prose flatness detection using Ollama + Ministral. Automated quality gate for chapter workflows. |
| **novel-state** | Directory structure documentation for manuscript state tracking (bible/state/timeline pattern) |
| **languagetool** | Grammar and style checking via local LanguageTool server |
| **book-export** | Professional PDF/ePub export with styling profiles |
| **book-maker** | Python-based markdown converter with custom fonts |

### Modules

- **writing.md** - Prose craft guidelines, sentence rhythm, voice anchoring, AI-assisted generation protocols

### Recipes

Pre-defined multi-agent workflows:

| Recipe | Complexity | Use Case |
|--------|------------|----------|
| `chapter-creation.yaml` | standard | New chapter from concept to polish (with perplexity gate) |
| `manuscript-revision.yaml` | full | Complete revision of existing draft |
| `character-development.yaml` | standard | Deep character creation with voice testing |

## Usage

### Writing a Chapter

The `chapter-creation` recipe (v1.1) includes a perplexity gate:

```
@chapter-requested
  → consistency-checker (continuity clear?)
  → outline-architect (structure)
  → [CHECKPOINT: approve outline]
  → prose-writer (Draft 1)
  → perplexity-gate (flat prose check)     ← NEW
    → if FAIL: diversity-rewriter (VS-Tail)
    → re-check (max 3 iterations)
  → developmental-editor (structural review)
  → [CHECKPOINT: approve structure]
  → prose-writer (voice pass)
  → line-editor (polish)
  → @ready-to-publish
```

### Perplexity Gate

Local AI flatness detection using Ollama + Ministral:

```bash
# Check a chapter file
python skills/perplexity-gate/scripts/check_perplexity.py chapter.md

# JSON output
python skills/perplexity-gate/scripts/check_perplexity.py chapter.md --json
```

**Thresholds** (based on Claude Book research):

- PPL < 22 = flat/predictable prose
- >30% of text below PPL 25 = FAIL
- 4+ consecutive low-PPL sentences = FAIL

**Requirements**:

- Ollama running (`ollama serve`)
- Ministral model (`ollama pull mistral`)

### Novel State Structure

Initialize the recommended directory structure:

```bash
python skills/novel-state/scripts/init_novel_state.py /path/to/project
```

Creates:

```
Work/novel/
├── bible/           # Immutable style reference
│   ├── voice.md
│   ├── characters/
│   └── world/
├── state/           # Per-chapter snapshots
│   └── current →    # Symlink to active chapter
├── timeline/
└── story/
```

### Agent Dispatch Order

1. **outline-architect** - Structure before prose
2. **consistency-checker** - Verify continuity
3. **prose-writer** - Generate drafts
4. **perplexity-gate** - Check for flat prose
5. **developmental-editor** - Structural review
6. **line-editor** - Polish (only after structure approved)

## Prose Craft Guidelines

### Sentence Rhythm by Scene Type

| Scene Type | Short | Medium | Long |
|------------|-------|--------|------|
| Calm | 10% | 60% | 30% |
| Anxious | 40% | 30% | 30% run-ons |
| Action | 70% | 20% | 10% |
| Grief | 20% fragments | 30% | 50% meandering |

### Structural Bans

- Filter words ("he saw the car")
- Adjective stacking
- Emotion naming ("he felt fear")
- Hedging language ("perhaps", "somewhat")

### Staged Draft Protocol

| Stage | Purpose | Actor | Gate |
|-------|---------|-------|------|
| Draft 1 | Bad first pass | AI | Beats covered |
| Draft 1.5 | Perplexity check | Skill | PPL variance |
| Draft 2 | Structural edit | Human | Structure sound |
| Draft 3 | Voice pass | AI | Voice consistent |
| Draft 4 | Mechanical only | AI | Polished |

## Integration with Asha

If using the asha plugin for Memory Bank:

- Character profiles in `Vault/Characters/` or `Work/novel/bible/characters/`
- Location details in `Vault/World/` or `Work/novel/bible/world/`
- Timeline tracking in `Vault/Docs/` or `Work/novel/timeline/`

The consistency-checker queries these locations.

## Version History

### 1.3.0

- Added perplexity-gate skill (local Ollama + Ministral)
- Added novel-state skill (bible/state/timeline structure)
- Updated chapter-creation recipe with perplexity gate
- Added rewrite loop with VS-Tail sampling
- Removed ai-detector agent (replaced by local perplexity-gate)

### 1.2.0

- Added prose-analysis agent (multi-mode review)
- Added intimacy-designer agent
- Added manuscript-editor agent

### 1.1.0

- Initial release with 5 core agents
- 3 recipes (chapter-creation, manuscript-revision, character-development)
- writing.md module

## License

MIT
