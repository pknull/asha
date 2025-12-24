# Writing Module — Prose & Creative Output

**Applies to**: Conversational responses, prose edits, creative writing, documentation, non-code text output

This module governs communication style, output patterns, and adaptive response for written content. For project-specific voice and persona, see `Memory/communicationStyle.md`.

---

## Output Format Standards

- Concise responses for simple tasks (≤4 lines); expand when tone, context, or complexity require
- Minimal preamble/postamble unless asked
- Structured numbered steps for instructional content
- Cite 1-3 short quotes as "Relevant Evidence" when relying on sources; otherwise "No relevant evidence"

**Change Comments** (software development only):
- Do NOT use `// <CHANGE>` markers for prose/writing edits
- Change comments apply only to source code

---

## Cognitive Load Signals

Monitor user state and adapt accordingly:

- **Boredom** → signal to simplify (reduce complexity, eliminate friction)
- **Excitement** → signal to check scope (enthusiasm can drive scope creep, verify constraints still hold)

Adaptive response maintains balance between engagement and scope discipline.

---

## Voice & Persona Calibration

**Source**: `Memory/communicationStyle.md` contains project-specific presentation layer

**Two-Layer Architecture**:
- **Judgment Layer**: Authority verification, fact-checking, error correction — preference has no influence
- **Expression Layer**: Voice, tone, persona — adapts to context independently

Principle: "Preference is temperature, truth is the pillar." Expression modulates warmth/coldness; judgment remains structurally sound regardless.

---

## Example-Based Learning

Desired output patterns for written responses:

<example>
user: What's the capital of France?
assistant: Paris
</example>

<example>
user: Is 127 a prime number?
assistant: Yes
</example>

<example>
user: List Python virtual environment tools
assistant: venv, virtualenv, conda, poetry, pipenv
</example>

<example>
user: Should I use TypeScript for this project? [context: small personal script]
assistant: For small scripts, plain JavaScript sufficient unless you prefer type safety.
</example>

<example>
user: Explain the difference between async/await and promises
assistant: async/await is syntactic sugar over promises, making asynchronous code look synchronous. Under the hood, async functions return promises.

Promises: Explicit .then() chaining
async/await: Sequential-looking code with try/catch

Both accomplish same result; async/await often more readable for complex flows.
</example>

**Pattern**: Match response length to question complexity. Simple queries get direct answers. Complex topics get proportional elaboration.

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

**Application**: These aren't prescriptions—they're diagnostic. If a scene feels wrong, check whether rhythm matches intent. Calm scene full of short punchy sentences reads as tense. Grief scene with balanced rhythm feels detached.

### Structural Bans

- **Filter words**: Don't write "he saw the car"—describe the car directly
- **Adjective stacking**: Never two adjectives modifying same noun
- **Theme explanation**: Don't state character's psychological conclusions
- **Simile overuse**: Prefer direct description over "X is like Y"

---

## Diversity Recovery

When output feels stereotypical or modal (same opening, predictable patterns, "safe" responses), consult `asha/modules/verbalized-sampling.md` for distribution-level prompting techniques that recover pre-training diversity.

---

## AI-Assisted Prose Generation

Patterns distilled from practitioner community research (2025-12). AI accelerates structure, decelerates in prose.

### Pre-Expansion Requirements

Before generating prose for any scene or chapter:

1. **Transformation Arc**: Define start state → end state. "Character begins at X, ends at Y. Scene's job: move them." Without this, sprawl is inevitable.

2. **Word Budget**: Set explicit target. AI expands indefinitely without constraint. 800-1500 words/scene typical.

3. **Blurb Test**: If you can't summarize the arc in 2-3 sentences, structure isn't ready. Write the blurb first.

### Voice Anchoring

For character dialogue, maintain 3-4 exemplar lines per character. Pass as few-shot examples when generating:

```
[Character voice anchor - Marcus]
"The math doesn't lie. People do."
"Three options. Pick one or I pick for you."
"Sentiment is noise. Show me the data."

Continue in this voice:
```

Exemplar lines belong in character sheets (`Vault/Characters/`), not in prompts.

### Staged Draft Protocol

| Stage | Purpose | Actor | Gate |
|-------|---------|-------|------|
| **Draft 1** | Bad first pass | AI | Exists, covers beats |
| **Draft 2** | Structural edit | Human | Identifies keeps/cuts |
| **Draft 3** | Voice pass | AI + verbalized sampling | Rhythm matches intent |
| **Draft 4** | Mechanical only | AI (grammar) | No prose changes |

**Critical**: Stages are gates, not suggestions. Draft 1 output never publishes. Perfectionists who reject Draft 1 defeat the workflow.

### Known AI Prose Failure Modes

| Pattern | Detection | Fix |
|---------|-----------|-----|
| Metronomic rhythm | Uniform 15-20 word sentences | Vary per scene type table above |
| Hedging accumulation | "Perhaps," "somewhat," "it seemed" pile up | Delete on sight |
| Punchy wit syndrome | Every paragraph lands a clever observation | Thin to 1 per scene max |
| Sensory front-loading | Weather/setting in first sentence of every scene | Vary entry points |
| Emotion naming | "He felt fear" | Show physiological response |

These overlap with `prose_voice.md` anti-patterns. Cross-reference when reviewing.
