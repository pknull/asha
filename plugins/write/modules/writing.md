# Writing Module — Prose & Creative Output

**Applies to**: Fiction writing, creative prose, narrative development, storytelling

This module governs prose craft, voice management, and adaptive creative output. For project-specific voice and persona, see `Memory/communicationStyle.md`.

---

## Prose Craft: Sentence Rhythm

For fiction and narrative prose, sentence length distribution signals emotional register. Match rhythm to scene intent:

| Scene Type | Short | Medium | Long/Run-on | Character |
|------------|-------|--------|-------------|-----------|
| **Calm** | 10% | 60% | 30% long | Steady, breathing room |
| **Anxious** | 40% | 30% | 30% run-ons | Jagged, accelerating |
| **Grief** | 20% fragments | 30% | 50% meandering | Slow, heavy, circular |
| **Action** | 70% | 20% | 10% single-word | Staccato, punchy |
| **Confusion** | uneven | fragments | run-ons + questions | Chaotic, disorienting |
| **Intimacy** | 30% | 50% | 20% fragments | Flowing but punctuated |

**Application**: These aren't prescriptions—they're diagnostic. If a scene feels wrong, check whether rhythm matches intent.

---

## Structural Bans

- **Filter words**: Don't write "he saw the car"—describe the car directly
- **Adjective stacking**: Never two adjectives modifying same noun
- **Theme explanation**: Don't state character's psychological conclusions
- **Simile overuse**: Prefer direct description over "X is like Y"

---

## Voice & Persona Calibration

**Source**: `Memory/communicationStyle.md` contains project-specific presentation layer

**Two-Layer Architecture**:
- **Judgment Layer**: Authority verification, fact-checking, error correction
- **Expression Layer**: Voice, tone, persona — adapts to context independently

Principle: Expression modulates warmth/coldness; judgment remains structurally sound regardless.

---

## AI-Assisted Prose Generation

AI accelerates structure, decelerates in prose.

### Pre-Expansion Requirements

Before generating prose for any scene or chapter:

1. **Transformation Arc**: Define start state → end state. Without this, sprawl is inevitable.

2. **Word Budget**: Set explicit target. AI expands indefinitely without constraint. 800-1500 words/scene typical.

3. **Blurb Test**: If you can't summarize the arc in 2-3 sentences, structure isn't ready.

### Voice Anchoring

For character dialogue, maintain 3-4 exemplar lines per character:

```
[Character voice anchor - Marcus]
"The math doesn't lie. People do."
"Three options. Pick one or I pick for you."
"Sentiment is noise. Show me the data."

Continue in this voice:
```

Exemplar lines belong in character sheets, not in prompts.

### Staged Draft Protocol

| Stage | Purpose | Actor | Gate |
|-------|---------|-------|------|
| **Draft 1** | Bad first pass | AI | Exists, covers beats |
| **Draft 2** | Structural edit | Human | Identifies keeps/cuts |
| **Draft 3** | Voice pass | AI + sampling | Rhythm matches intent |
| **Draft 4** | Mechanical only | AI (grammar) | No prose changes |

**Critical**: Stages are gates, not suggestions. Draft 1 output never publishes.

### Known AI Prose Failure Modes

| Pattern | Detection | Fix |
|---------|-----------|-----|
| Metronomic rhythm | Uniform 15-20 word sentences | Vary per scene type |
| Hedging accumulation | "Perhaps," "somewhat" pile up | Delete on sight |
| Punchy wit syndrome | Every paragraph clever | Thin to 1 per scene max |
| Sensory front-loading | Weather in first sentence | Vary entry points |
| Emotion naming | "He felt fear" | Show physiological response |

---

## Quality Gates for Writing

| Phase | Gate | Agent | Blocking |
|-------|------|-------|----------|
| Pre-Draft | Structure Approved | outline-architect | Yes |
| Pre-Draft | Continuity Clear | consistency-checker | Yes |
| Post-Draft 1 | Beats Covered | prose-writer | Yes |
| Post-Draft 2 | Structure Sound | developmental-editor | Yes |
| Post-Draft 3 | Prose Polished | line-editor | Yes |

---

## Swarm Recipes

Pre-defined multi-agent workflows in `recipes/` directory:

| Recipe | Use Case |
|--------|----------|
| `chapter-creation.yaml` | New chapter from outline to polished prose |
| `manuscript-revision.yaml` | Full revision pass on existing draft |
| `character-development.yaml` | Deep character creation with voice testing |

See `recipes/README.md` for full schema.
