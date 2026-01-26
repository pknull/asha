# Write Plugin

Creative writing workflows for fiction development: prose craft, worldbuilding, editing, and storytelling agents.

## Installation

```bash
/plugin install write@asha-marketplace
```

## Features

### Agents

- **outline-architect** - Story structure specialist. Builds beat sheets, chapter outlines, and narrative architecture.
- **prose-writer** - Draft generation specialist. Expands outlines into prose while maintaining voice and rhythm.
- **consistency-checker** - Worldbuilding and continuity specialist. Tracks characters, timelines, locations, and lore.
- **developmental-editor** - Structural analysis specialist. Evaluates arcs, pacing, and thematic coherence.
- **line-editor** - Prose quality specialist. Sentence-level craft, rhythm, word choice, and mechanical polish.

### Modules

- **writing.md** - Prose craft guidelines, sentence rhythm, voice anchoring, AI-assisted generation protocols

### Recipes

Pre-defined multi-agent workflows:

| Recipe | Complexity | Use Case |
|--------|------------|----------|
| `chapter-creation.yaml` | standard | New chapter from concept to polish |
| `manuscript-revision.yaml` | full | Complete revision of existing draft |
| `character-development.yaml` | standard | Deep character creation with voice testing |

## Usage

### Writing a Chapter

The `chapter-creation` recipe follows this flow:

```
@chapter-requested
  → consistency-checker (continuity clear?)
  → outline-architect (structure)
  → [CHECKPOINT: approve outline]
  → prose-writer (Draft 1)
  → developmental-editor (structural review)
  → [CHECKPOINT: approve structure]
  → prose-writer (voice pass)
  → line-editor (polish)
  → @ready-to-publish
```

### Agent Dispatch Order

1. **outline-architect** - Structure before prose
2. **consistency-checker** - Verify continuity
3. **prose-writer** - Generate drafts
4. **developmental-editor** - Structural review
5. **line-editor** - Polish (only after structure approved)

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

| Stage | Purpose | Actor |
|-------|---------|-------|
| Draft 1 | Bad first pass | AI |
| Draft 2 | Structural edit | Human |
| Draft 3 | Voice pass | AI |
| Draft 4 | Mechanical only | AI |

## Integration with Asha

If using the asha plugin for Memory Bank:

- Character profiles in `Vault/Characters/`
- Location details in `Vault/World/`
- Timeline tracking in `Vault/Docs/`

The consistency-checker queries these locations.

## License

MIT
